"""相似圖片/影片偵測頁面。與「重複檔案」(位元組完全相同)刻意分離:這裡用感知雜湊做
機率性相似判斷,會有誤判,故 UI 一律呈現候選 + 縮圖 + (影片)相似片段,由使用者人工複核後刪除。
"""

import os
import subprocess

from PyQt6.QtCore import QThread, Qt
from PyQt6.QtGui import QFontMetrics, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import analysis, theme
from ..utils.fs import display_path, format_size, list_drives
from ..widgets.neon_progress import NeonProgressBar
from ..workers import SimilarityWorker
from .common import HUGE_FILE_THRESHOLD, ChipRow, FolderPicker, confirm_delete, safe_trash_delete

PATH_ELIDE_WIDTH = 480
TYPE_FILTERS = [("image", "圖片"), ("video", "影片")]

# 圖片:(標籤, Hamming 門檻)——兩張圖的 64-bit dHash 指紋逐 bit 比對,不同的 bit 數 <= 門檻才算相似。
IMAGE_STRICTNESS_OPTIONS = [
    ("寬鬆", 14),
    ("標準", 10),
    ("嚴格", 6),
]

# 影片:(標籤, 每幀 Hamming 門檻, 最短連續相似秒數)。每幀門檻決定「這一幀算不算同一畫面」,
# 最短秒數決定「連續相似要多久才不算巧合」(過濾共用片頭/黑畫面之類的雜訊)。
# 標準檔位的數值維持跟改版前一致,行為不變。「非常寬鬆」只把秒數壓到 4,每幀門檻沿用「寬鬆」
# 的 14,不跟著再放寬——兩個維度同時最鬆會讓巧合誤判疊加,秒數已經很短了沒必要雙重放寬。
VIDEO_STRICTNESS_OPTIONS = [
    ("非常寬鬆", 14, 4),
    ("寬鬆", 14, 12),
    ("標準", 10, 20),
    ("嚴格", 6, 30),
]


def _image_strictness_label(name: str, threshold: int) -> str:
    return f"{name}(64 bit 指紋最多容許 {threshold} bit 不同)"


def _video_strictness_label(name: str, frame_threshold: int, min_match_seconds: int) -> str:
    return f"{name}(每幀最多容許 {frame_threshold} bit 不同,至少連續重疊 {min_match_seconds} 秒)"


class SimilarityPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None
        self._groups: list[list[dict]] = []
        self._group_segments: list[list[str]] = []

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

        title = QLabel("相似圖片/影片偵測")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel(
            "用感知雜湊找「內容相似」的檔案(縮放、轉檔、重壓縮、不同畫質/FPS、剪輯過的影片)。"
            "會有誤判,結果僅供人工複核;抓不到裁切或加塗鴉的圖片。"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._type_chips = ChipRow(items=TYPE_FILTERS, default_checked={"image"}, exclusive=True)
        self._type_chips.selection_changed.connect(self._on_type_changed)
        layout.addWidget(self._type_chips)

        strict_row = QHBoxLayout()
        strict_label = QLabel("相似程度:")
        strict_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        strict_row.addWidget(strict_label)

        self._image_strict_combo = QComboBox()
        for name, threshold in IMAGE_STRICTNESS_OPTIONS:
            self._image_strict_combo.addItem(_image_strictness_label(name, threshold))
        self._image_strict_combo.setCurrentIndex(1)  # 標準
        strict_row.addWidget(self._image_strict_combo)

        self._video_strict_combo = QComboBox()
        for name, frame_threshold, min_match_seconds in VIDEO_STRICTNESS_OPTIONS:
            self._video_strict_combo.addItem(_video_strictness_label(name, frame_threshold, min_match_seconds))
        self._video_strict_combo.setCurrentIndex(2)  # 標準
        strict_row.addWidget(self._video_strict_combo)

        strict_row.addStretch(1)
        layout.addLayout(strict_row)

        sysdrive = os.environ.get("SystemDrive", "C:").upper() + "\\"
        drives = list_drives()
        self._drive_chips = ChipRow(items=[(d, d.rstrip("\\")) for d in drives], default_checked={sysdrive})
        layout.addWidget(self._drive_chips)

        self._folder_picker = FolderPicker(
            hint="指定資料夾範圍(可選):新增後只掃描這些資料夾(含子目錄),不新增則掃描上方勾選的磁碟。"
            "影片解碼取樣很耗時,強烈建議縮小到想比對的資料夾。"
        )
        layout.addWidget(self._folder_picker)

        layout.addStretch(1)

        start_btn = QPushButton("開始掃描")
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

        self._on_type_changed()  # 依預設勾選的類型,只顯示對應的相似程度下拉
        return page

    def _on_type_changed(self) -> None:
        mode = self._current_mode()
        self._image_strict_combo.setVisible(mode == "image")
        self._video_strict_combo.setVisible(mode == "video")

    def _current_mode(self) -> str:
        keys = self._type_chips.checked_keys()
        return keys[0] if keys else "image"

    # -------------------------------------------------------------- SCANNING
    def _build_scanning_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.addStretch(1)

        self._phase_label = QLabel("")
        self._phase_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._phase_label.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-family: Consolas;")
        layout.addWidget(self._phase_label)

        self._progress_bar = NeonProgressBar()
        layout.addWidget(self._progress_bar)

        self._phase_path_label = QLabel("")
        self._phase_path_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._phase_path_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas; font-size: 9pt;")
        layout.addWidget(self._phase_path_label)

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
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(8)

        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet(
            f"color: {theme.NEON_PINK}; font-family: Consolas; font-size: 12pt; font-weight: bold;"
        )
        layout.addWidget(self._summary_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["檔案", "修改日期 / 操作"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(1, 260)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        self._tree.itemSelectionChanged.connect(self._update_preview)
        self._tree.itemChanged.connect(self._on_item_changed)
        splitter.addWidget(self._tree)

        self._preview_panel = self._build_preview_panel()
        splitter.addWidget(self._preview_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        bottom = QFrame()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        self._guard_label = QLabel("")
        self._guard_label.setStyleSheet(f"color: {theme.DANGER};")
        bottom_layout.addWidget(self._guard_label)
        bottom_layout.addStretch(1)

        rescan_btn = QPushButton("重新掃描")
        rescan_btn.clicked.connect(lambda: self._stack.setCurrentWidget(self._setup_page))
        bottom_layout.addWidget(rescan_btn)

        self._delete_checked_btn = QPushButton("刪除勾選項目")
        self._delete_checked_btn.clicked.connect(self._delete_checked)
        bottom_layout.addWidget(self._delete_checked_btn)
        layout.addWidget(bottom)
        return page

    def _build_preview_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(f"background-color: {theme.BG_PANEL};")
        layout = QVBoxLayout(panel)
        self._preview_image = QLabel("選取檔案以預覽")
        self._preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_image.setMinimumHeight(200)
        self._preview_image.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self._preview_meta = QLabel("")
        self._preview_meta.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas; font-size: 9pt;")
        self._preview_meta.setWordWrap(True)
        layout.addWidget(self._preview_image)
        layout.addWidget(self._preview_meta)
        layout.addStretch(1)
        return panel

    # ----------------------------------------------------------------- SCAN
    def _start_scan(self) -> None:
        folders = self._folder_picker.selected_folders()
        targets = folders if folders else self._drive_chips.checked_keys()
        if not targets:
            return
        mode = self._current_mode()
        if mode == "video":
            _name, threshold, min_match_seconds = VIDEO_STRICTNESS_OPTIONS[self._video_strict_combo.currentIndex()]
        else:
            _name, threshold = IMAGE_STRICTNESS_OPTIONS[self._image_strict_combo.currentIndex()]
            min_match_seconds = 20  # 圖片模式用不到,給個值即可

        self._phase_label.setText("正在計算指紋…")
        self._progress_bar.set_indeterminate(True)
        self._phase_path_label.setText("")
        self._stack.setCurrentWidget(self._scanning_page)

        self._thread = QThread(self)
        self._worker = SimilarityWorker(targets, mode, threshold, min_match_seconds)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._worker.progress.connect(self._on_progress)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_scan_finished)
        self._thread.start()

    def _cancel_scan(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _on_progress(self, phase: int, done: int, total: int, path: str) -> None:
        metrics = QFontMetrics(self._phase_path_label.font())
        self._phase_path_label.setText(metrics.elidedText(path, Qt.TextElideMode.ElideMiddle, PATH_ELIDE_WIDTH))
        if phase == 1:
            self._phase_label.setText(f"第 1/2 階段:計算指紋(已處理 {done} 個)")
            self._progress_bar.set_indeterminate(True)
        else:
            self._phase_label.setText(f"第 2/2 階段:相似比對({done}/{total})")
            if total:
                self._progress_bar.set_indeterminate(False)
                self._progress_bar.setValue(min(int(done / total * 100), 100))
            else:
                self._progress_bar.set_indeterminate(True)

    def _on_error(self, message: str) -> None:
        QMessageBox.warning(self, "相似偵測無法執行", message)

    def _on_scan_finished(self, groups: list) -> None:
        cancelled = bool(self._worker and self._worker.cancelled)
        self._thread = None
        self._worker = None
        if cancelled:
            self._stack.setCurrentWidget(self._setup_page)
            return

        self._groups = []
        self._group_segments = []
        for group in groups:
            entries = []
            for path in group["paths"]:
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                entries.append({"path": path, "size": st.st_size, "mtime": st.st_mtime})
            if len(entries) >= 2:
                self._groups.append(entries)
                self._group_segments.append(group.get("segments", []))

        self._populate_tree()
        self._stack.setCurrentWidget(self._results_page)

    def _populate_tree(self) -> None:
        self._tree.itemChanged.disconnect(self._on_item_changed)
        self._tree.clear()

        for group, segments in zip(self._groups, self._group_segments):
            n = len(group)
            largest = max(e["size"] for e in group)
            top = QTreeWidgetItem([f"{n} 個相似檔案(最大 {format_size(largest)})", ""])
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self._tree.addTopLevelItem(top)

            keep_row = QWidget()
            keep_layout = QHBoxLayout(keep_row)
            keep_layout.setContentsMargins(2, 0, 2, 0)
            keep_layout.setSpacing(4)
            btn_style = "padding: 2px 6px; font-size: 8pt;"
            oldest_btn = QPushButton("保留最舊")
            oldest_btn.setStyleSheet(btn_style)
            oldest_btn.clicked.connect(lambda _c, g=top: self._keep_extreme(g, keep_oldest=True))
            newest_btn = QPushButton("保留最新")
            newest_btn.setStyleSheet(btn_style)
            newest_btn.clicked.connect(lambda _c, g=top: self._keep_extreme(g, keep_oldest=False))
            keep_layout.addWidget(oldest_btn)
            keep_layout.addWidget(newest_btn)
            self._tree.setItemWidget(top, 1, keep_row)

            for entry in group:
                child = QTreeWidgetItem([display_path(entry["path"]), analysis.format_relative_time(entry["mtime"])])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
                child.setData(0, Qt.ItemDataRole.UserRole, entry)
                top.addChild(child)

            # 影片:把相似片段區間掛成不可勾選的說明列
            for seg in segments:
                info = QTreeWidgetItem([f"↳ {seg}", ""])
                info.setFlags(info.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                info.setForeground(0, self._dim_brush())
                top.addChild(info)

            top.setExpanded(True)

        self._summary_label.setText(f"共 {len(self._groups)} 組相似檔案")
        self._tree.itemChanged.connect(self._on_item_changed)
        self._check_guard()

    def _dim_brush(self):
        from PyQt6.QtGui import QColor

        return QColor(theme.TEXT_DIM)

    def _keep_extreme(self, group_item: QTreeWidgetItem, keep_oldest: bool) -> None:
        children = [
            group_item.child(i)
            for i in range(group_item.childCount())
            if group_item.child(i).data(0, Qt.ItemDataRole.UserRole)
        ]
        if not children:
            return
        key = lambda c: c.data(0, Qt.ItemDataRole.UserRole)["mtime"]
        target = min(children, key=key) if keep_oldest else max(children, key=key)
        for c in children:
            c.setCheckState(0, Qt.CheckState.Unchecked if c is target else Qt.CheckState.Checked)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        self._check_guard()

    def _check_guard(self) -> None:
        """防呆:不允許整組全勾。"""
        any_full_group = False
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            children = [
                top.child(j) for j in range(top.childCount()) if top.child(j).data(0, Qt.ItemDataRole.UserRole)
            ]
            if children and all(c.checkState(0) == Qt.CheckState.Checked for c in children):
                any_full_group = True
                break
        if any_full_group:
            self._guard_label.setText("每組至少保留一個")
            self._delete_checked_btn.setEnabled(False)
        else:
            self._guard_label.setText("")
            self._delete_checked_btn.setEnabled(True)

    def _update_preview(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            return
        data = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not data:
            self._preview_image.setText("")
            self._preview_image.setPixmap(QPixmap())
            self._preview_meta.setText("")
            return

        path = data["path"]
        ext = os.path.splitext(path)[1].lower()
        if ext in analysis.IMAGE_EXTS:
            pix = QPixmap(path)
            if pix.isNull():
                self._preview_image.setPixmap(QPixmap())
                self._preview_image.setText("無法預覽")
            else:
                scaled = pix.scaled(
                    320, 320, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
                self._preview_image.setPixmap(scaled)
                self._preview_image.setText("")
        else:
            self._preview_image.setPixmap(QPixmap())
            self._preview_image.setText("(影片不支援預覽)")

        self._preview_meta.setText(
            f"{os.path.basename(path)}\n大小:{format_size(data['size'])}\n"
            f"修改日期:{analysis.format_relative_time(data['mtime'])}\n{path}"
        )

    def _iter_checked_entries(self):
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                if data and child.checkState(0) == Qt.CheckState.Checked:
                    yield child, data

    def _delete_checked(self) -> None:
        entries = list(self._iter_checked_entries())
        if not entries:
            return
        total_size = sum(d["size"] for _c, d in entries)
        huge = any(d["size"] > HUGE_FILE_THRESHOLD for _c, d in entries)
        msg = f"確定要刪除勾選的 {len(entries)} 個檔案嗎?\n總計 {format_size(total_size)}"
        if not confirm_delete(self, "刪除相似檔案", msg, huge_file=huge):
            return

        failures = []
        for child, data in entries:
            ok, message = safe_trash_delete(data["path"], data["size"])
            if ok:
                child.parent().removeChild(child)
            else:
                failures.append(f"{data['path']}: {message}")

        for i in reversed(range(self._tree.topLevelItemCount())):
            top = self._tree.topLevelItem(i)
            remaining = [
                top.child(j) for j in range(top.childCount()) if top.child(j).data(0, Qt.ItemDataRole.UserRole)
            ]
            if len(remaining) < 2:
                self._tree.takeTopLevelItem(i)

        self._check_guard()
        if failures:
            QMessageBox.warning(self, "部分檔案未刪除", "\n".join(failures[:20]))

    def _show_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        menu = QMenu(self)
        copy_action = menu.addAction("複製路徑")
        open_action = menu.addAction("開啟位置")
        action = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if action == copy_action:
            from PyQt6.QtWidgets import QApplication

            QApplication.clipboard().setText(data["path"])
        elif action == open_action and os.path.exists(data["path"]):
            subprocess.Popen(["explorer", "/select,", data["path"]])

    def shutdown(self) -> None:
        if self._worker:
            self._worker.cancel()
        if self._thread:
            self._thread.wait(2000)
