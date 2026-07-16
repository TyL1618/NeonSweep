"""空間視覺化(treemap):用面積直觀呈現哪個資料夾佔空間,補足大檔案掃描器抓不到
「一堆小檔案加起來很肥的資料夾」的盲點。純檢視工具,不提供刪除(要刪請到大檔案/重複頁面)。
"""

import os
import subprocess

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import theme, treemap
from ..analysis import classify
from ..utils.fs import format_size, list_drives
from ..widgets.neon_progress import NeonProgressBar
from ..workers import TreeSizeWorker
from .common import ChipRow, FolderPicker
from PyQt6.QtCore import QThread

# 類型 -> 填色(調暗的霓虹色系,深到白字仍可讀)。資料夾與聚合節點另有專色。
_CAT_COLORS = {
    "AI 模型": "#a83277",
    "影片": "#2f7d8a",
    "映像檔": "#6a4c93",
    "壓縮檔": "#4a7c59",
    "遊戲": "#8a6d2f",
    "其他": "#3a3a46",
}
_FOLDER_COLOR = "#2e5a88"
_AGG_COLOR = "#24242c"
_PAD = 6


def _stable_variation(name: str) -> int:
    """由名稱推出 -14..+14 的亮度微調,讓相鄰同類別矩形能區分開(deterministic)。"""
    return (sum(bytearray(name.encode("utf-8", "ignore"))) % 29) - 14


def _node_color(node) -> QColor:
    if node.get("aggregate"):
        return QColor(_AGG_COLOR)
    if node.get("is_dir"):
        base = _FOLDER_COLOR
    else:
        base = _CAT_COLORS.get(classify(node["path"])[0], _CAT_COLORS["其他"])
    c = QColor(base)
    h, s, light, a = c.getHsl()
    c.setHsl(h, s, max(0, min(255, light + _stable_variation(node.get("name", "")))), a)
    return c


class TreemapView(QWidget):
    """自訂繪圖區:對「當前層級」的子節點做 squarified treemap,點資料夾下鑽、麵包屑回上層。"""

    navigated = pyqtSignal(list)   # 目前的節點堆疊(root..current),供頁面重建麵包屑

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stack: list = []      # root..current
        self._rects: list = []      # list[(node, QRectF)]
        self.setMouseTracking(True)
        self.setMinimumHeight(320)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def set_root(self, root) -> None:
        self._stack = [root] if root else []
        self._relayout()
        self.update()
        self.navigated.emit(list(self._stack))

    def jump_to(self, index: int) -> None:
        if 0 <= index < len(self._stack):
            self._stack = self._stack[: index + 1]
            self._relayout()
            self.update()
            self.navigated.emit(list(self._stack))

    def _current(self):
        return self._stack[-1] if self._stack else None

    def _drill(self, node) -> None:
        if node.get("is_dir") and node.get("children"):
            self._stack.append(node)
            self._relayout()
            self.update()
            self.navigated.emit(list(self._stack))

    def _relayout(self) -> None:
        self._rects = []
        cur = self._current()
        if not cur:
            return
        w = max(self.width() - 2 * _PAD, 1)
        h = max(self.height() - 2 * _PAD, 1)
        kids = treemap.top_children(cur)
        for node, (x, y, rw, rh) in treemap.squarify(kids, (_PAD, _PAD, w, h)):
            self._rects.append((node, QRectF(x, y, rw, rh)))

    def resizeEvent(self, event) -> None:
        self._relayout()
        super().resizeEvent(event)

    def _node_at(self, pos):
        for node, r in self._rects:
            if r.contains(pos):
                return node, r
        return None, None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.BG))
        if not self._rects:
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "此範圍沒有可顯示的內容")
            painter.end()
            return

        fm = QFontMetrics(painter.font())
        for node, r in self._rects:
            painter.fillRect(r, _node_color(node))
            painter.setPen(QColor(theme.BG))
            painter.drawRect(r)
            if r.width() > 48 and r.height() > 20:
                painter.setPen(QColor(theme.TEXT_MAIN))
                name = fm.elidedText(node["name"], Qt.TextElideMode.ElideMiddle, int(r.width()) - 8)
                text = f"{name}\n{format_size(node['size'])}"
                painter.drawText(
                    r.adjusted(4, 2, -4, -2),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap),
                    text,
                )
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            node, _ = self._node_at(event.position())
            if node:
                self._drill(node)

    def mouseMoveEvent(self, event) -> None:
        node, _ = self._node_at(event.position())
        if node:
            label = node.get("path") or node["name"]
            self.setToolTip(f"{label}\n{format_size(node['size'])}")
        else:
            self.setToolTip("")

    def _show_context_menu(self, pos) -> None:
        node, _ = self._node_at(pos)
        if not node or not node.get("path"):
            return
        menu = QMenu(self)
        open_action = menu.addAction("開啟位置")
        action = menu.exec(self.mapToGlobal(pos))
        if action == open_action and os.path.exists(node["path"]):
            subprocess.Popen(["explorer", "/select,", node["path"]])


class TreemapPage(QWidget):
    """空間視覺化頁面:三段式(setup / scanning / results)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        outer.addWidget(self._stack)

        self._setup_page = self._build_setup_page()
        self._scanning_page = self._build_scanning_page()
        self._results_page = self._build_results_page()
        for p in (self._setup_page, self._scanning_page, self._results_page):
            self._stack.addWidget(p)
        self._stack.setCurrentWidget(self._setup_page)

    # ------------------------------------------------------------------ SETUP
    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(14)

        title = QLabel("空間視覺化")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel("以面積呈現各資料夾佔用的空間,點方塊可往下鑽,補足大檔案掃描抓不到的「小檔案堆積」")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        sysdrive = os.environ.get("SystemDrive", "C:").upper() + "\\"
        drives = list_drives()
        self._drive_chips = ChipRow(items=[(d, d.rstrip("\\")) for d in drives], default_checked={sysdrive})
        layout.addWidget(self._drive_chips)

        self._folder_picker = FolderPicker(
            hint="指定資料夾範圍(可選):新增後會改成只分析這些資料夾(含子目錄),不新增則分析上方勾選的磁碟。"
            "整碟掃描檔案很多時會較久,建議縮小到想看的資料夾。"
        )
        layout.addWidget(self._folder_picker)

        layout.addStretch(1)

        start_btn = QPushButton("開始分析")
        start_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.GRADIENT_PINK_BLUE}; color: white; "
            f"border: none; border-radius: 6px; padding: 10px 32px; font-weight: bold; }}"
        )
        start_btn.clicked.connect(self._start_scan)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(start_btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)
        return page

    # -------------------------------------------------------------- SCANNING
    def _build_scanning_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.addStretch(1)

        self._progress_bar = NeonProgressBar()
        self._progress_bar.set_indeterminate(True)
        layout.addWidget(self._progress_bar)

        self._count_label = QLabel("已掃描 0 個檔案")
        self._count_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._count_label.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-family: Consolas;")
        layout.addWidget(self._count_label)

        self._path_label = QLabel("")
        self._path_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._path_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas; font-size: 9pt;")
        layout.addWidget(self._path_label)

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self._cancel_scan)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(cancel_btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    # --------------------------------------------------------------- RESULTS
    def _build_results_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._breadcrumb_row = QHBoxLayout()
        self._breadcrumb_row.setSpacing(2)
        bar.addLayout(self._breadcrumb_row)
        bar.addStretch(1)
        rescan_btn = QPushButton("重新掃描")
        rescan_btn.clicked.connect(lambda: self._stack.setCurrentWidget(self._setup_page))
        bar.addWidget(rescan_btn)
        layout.addLayout(bar)

        self._view = TreemapView()
        self._view.navigated.connect(self._rebuild_breadcrumb)
        layout.addWidget(self._view, 1)
        return page

    def _rebuild_breadcrumb(self, stack: list) -> None:
        while self._breadcrumb_row.count():
            item = self._breadcrumb_row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for i, node in enumerate(stack):
            if i > 0:
                sep = QLabel("›")
                sep.setStyleSheet(f"color: {theme.TEXT_DIM};")
                self._breadcrumb_row.addWidget(sep)
            btn = QPushButton(node["name"])
            is_last = i == len(stack) - 1
            color = theme.NEON_PINK if is_last else theme.TEXT_DIM
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {color}; padding: 2px 6px; }}"
                f"QPushButton:hover {{ color: {theme.NEON_PINK_L}; }}"
            )
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c, idx=i: self._view.jump_to(idx))
            self._breadcrumb_row.addWidget(btn)

    # ----------------------------------------------------------------- SCAN
    def _start_scan(self) -> None:
        folders = self._folder_picker.selected_folders()
        targets = folders if folders else self._drive_chips.checked_keys()
        if not targets:
            return

        self._count_label.setText("已掃描 0 個檔案")
        self._path_label.setText("")
        self._stack.setCurrentWidget(self._scanning_page)

        self._thread = QThread(self)
        self._worker = TreeSizeWorker(targets)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_scan_finished)
        self._thread.start()

    def _cancel_scan(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _on_progress(self, count: int, path: str) -> None:
        self._count_label.setText(f"已掃描 {count} 個檔案")
        metrics = QFontMetrics(self._path_label.font())
        self._path_label.setText(metrics.elidedText(path, Qt.TextElideMode.ElideMiddle, 480))

    def _on_scan_finished(self, root) -> None:
        cancelled = bool(self._worker and self._worker.cancelled)
        self._thread = None
        self._worker = None
        if cancelled or root is None:
            self._stack.setCurrentWidget(self._setup_page)
            return
        self._view.set_root(root)
        self._stack.setCurrentWidget(self._results_page)

    def shutdown(self) -> None:
        if self._worker:
            self._worker.cancel()
        if self._thread:
            self._thread.wait(2000)
