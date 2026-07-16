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
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import analysis, theme
from ..utils.fs import display_path, format_size, list_drives, wrap_path_for_label
from ..widgets.neon_progress import NeonProgressBar
from ..workers import DupeWorker
from .common import HUGE_FILE_THRESHOLD, ChipRow, FolderPicker, confirm_delete, safe_trash_delete

PATH_ELIDE_WIDTH = 480
MIN_SIZE_OPTIONS = [("500 KB", 500 * 1024), ("1 MB", 1024 * 1024), ("10 MB", 10 * 1024 * 1024), ("100 MB", 100 * 1024 * 1024)]
TYPE_FILTERS = [
    ("all", "全部"),
    ("video", "影片"),
    ("image", "圖片"),
    ("audio", "音訊"),
]


class DupePage(QWidget):
    """重複檔案偵測(DEVDOC §8.3):三階段漏斗(大小分組 -> 前4KB快速雜湊 -> 全檔雜湊)。
    僅偵測位元組完全相同的檔案,不做感知雜湊/影片重編碼比對。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None
        self._groups: list[list[dict]] = []  # each: {path, size, mtime}

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

    # ------------------------------------------------------------------
    # SETUP
    # ------------------------------------------------------------------

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(14)

        title = QLabel("重複檔案偵測")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel("只偵測位元組完全相同的檔案,與檔名無關")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        sysdrive = os.environ.get("SystemDrive", "C:").upper() + "\\"
        drives = list_drives()
        self._drive_chips = ChipRow(items=[(d, d.rstrip("\\")) for d in drives], default_checked={sysdrive})
        layout.addWidget(self._drive_chips)

        self._folder_picker = FolderPicker(
            hint="指定資料夾範圍(可選):新增後會改成只掃描這些資料夾(含子目錄),不新增則掃描上方勾選的磁碟"
        )
        layout.addWidget(self._folder_picker)

        self._type_chips = ChipRow(items=TYPE_FILTERS, default_checked={"all"})
        layout.addWidget(self._type_chips)

        size_row = QHBoxLayout()
        size_label = QLabel("最小檔案門檻:")
        size_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self._min_size_combo = QComboBox()
        for label, _value in MIN_SIZE_OPTIONS:
            self._min_size_combo.addItem(label)
        self._min_size_combo.setCurrentIndex(1)  # 1 MB 預設
        size_row.addWidget(size_label)
        size_row.addWidget(self._min_size_combo)
        size_row.addStretch(1)
        layout.addLayout(size_row)

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

        return page

    # ------------------------------------------------------------------
    # SCANNING
    # ------------------------------------------------------------------

    def _build_scanning_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.addStretch(1)

        self._phase_label = QLabel("")
        self._phase_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._phase_label.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-family: Consolas;")
        layout.addWidget(self._phase_label)

        self._dupe_progress_bar = NeonProgressBar()
        layout.addWidget(self._dupe_progress_bar)

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

    def _selected_extensions(self) -> set | None:
        keys = self._type_chips.checked_keys()
        if not keys or "all" in keys:
            return None
        mapping = {"video": analysis.VIDEO_EXTS, "image": analysis.IMAGE_EXTS, "audio": analysis.AUDIO_EXTS}
        exts: set = set()
        for k in keys:
            exts |= mapping.get(k, set())
        return exts or None

    def _start_scan(self) -> None:
        folders = self._folder_picker.selected_folders()
        targets = folders if folders else self._drive_chips.checked_keys()
        if not targets:
            return
        extensions = self._selected_extensions()
        min_size = MIN_SIZE_OPTIONS[self._min_size_combo.currentIndex()][1]

        self._phase_label.setText("第 1/3 階段:比對檔案大小(已掃 0 檔)")
        self._dupe_progress_bar.set_indeterminate(True)
        self._phase_path_label.setText("")
        self._stack.setCurrentWidget(self._scanning_page)

        self._thread = QThread(self)
        self._worker = DupeWorker(targets, extensions, min_size)
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

    def _on_progress(self, phase: int, done: int, total: int, path: str) -> None:
        metrics = QFontMetrics(self._phase_path_label.font())
        elided = metrics.elidedText(path, Qt.TextElideMode.ElideMiddle, PATH_ELIDE_WIDTH)
        self._phase_path_label.setText(elided)
        if phase == 1:
            self._phase_label.setText(f"第 1/3 階段:比對檔案大小(已掃 {done} 檔)")
            self._dupe_progress_bar.set_indeterminate(True)
        elif phase == 2:
            self._phase_label.setText(f"第 2/3 階段:快速比對({total} 檔候選,已處理 {done})")
            self._dupe_progress_bar.set_indeterminate(True)
        else:
            self._phase_label.setText(f"第 3/3 階段:完整雜湊(第 {done}/{total} 檔)")
            self._dupe_progress_bar.set_indeterminate(False)
            if total:
                self._dupe_progress_bar.setValue(min(int(done / total * 100), 100))

    def shutdown(self) -> None:
        if self._worker:
            self._worker.cancel()
        if self._thread:
            self._thread.wait(2000)

    # ------------------------------------------------------------------
    # RESULTS
    # ------------------------------------------------------------------

    def _build_results_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(8)

        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet(f"color: {theme.NEON_PINK}; font-family: Consolas; font-size: 12pt; font-weight: bold;")
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
        # 檔案路徑沒有空白給 word-wrap 找斷點,不設這個的話整個面板會被撐到跟路徑一樣寬、
        # 分隔線怎麼拖都縮不小(見 wrap_path_for_label 的說明)。
        self._preview_meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._preview_image)
        layout.addWidget(self._preview_meta)
        layout.addStretch(1)
        return panel

    def _on_scan_finished(self, groups: list[list[str]]) -> None:
        cancelled = bool(self._worker and self._worker.cancelled)
        self._thread = None
        self._worker = None
        if cancelled:
            self._stack.setCurrentWidget(self._setup_page)
            return

        self._groups = []
        for group_paths in groups:
            entries = []
            for path in group_paths:
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                entries.append({"path": path, "size": st.st_size, "mtime": st.st_mtime})
            if len(entries) >= 2:
                self._groups.append(entries)

        self._populate_tree()
        self._stack.setCurrentWidget(self._results_page)

    def _populate_tree(self) -> None:
        self._tree.itemChanged.disconnect(self._on_item_changed)
        self._tree.clear()

        total_freed = 0
        for group in self._groups:
            size = group[0]["size"]
            n = len(group)
            freed = (n - 1) * size
            total_freed += freed
            top = QTreeWidgetItem([f"{n} 個相同檔案 × 每個 {format_size(size)},可省 {format_size(freed)}", ""])
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self._tree.addTopLevelItem(top)

            # 「保留最舊/最新」放在群組本身這一列(column 1),不要當成獨立浮動列,
            # 這樣操作對象一看就懂是這個群組,不會誤以為是某個檔案列的東西。
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

            names = [os.path.basename(e["path"]) for e in group]
            copy_style = [analysis.is_copy_style_name(n) for n in names]
            auto_check = any(copy_style) and not all(copy_style)

            for entry, is_copy in zip(group, copy_style):
                child = QTreeWidgetItem([display_path(entry["path"]), analysis.format_relative_time(entry["mtime"])])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if (auto_check and is_copy) else Qt.CheckState.Unchecked)
                child.setData(0, Qt.ItemDataRole.UserRole, entry)
                top.addChild(child)

            top.setExpanded(True)

        self._summary_label.setText(f"共 {len(self._groups)} 組重複,合計可釋放 {format_size(total_freed)}")
        self._tree.itemChanged.connect(self._on_item_changed)
        self._check_guard()

    def _keep_extreme(self, group_item: QTreeWidgetItem, keep_oldest: bool) -> None:
        children = [group_item.child(i) for i in range(group_item.childCount()) if group_item.child(i).data(0, Qt.ItemDataRole.UserRole)]
        if not children:
            return
        target = min(children, key=lambda c: c.data(0, Qt.ItemDataRole.UserRole)["mtime"]) if keep_oldest else \
            max(children, key=lambda c: c.data(0, Qt.ItemDataRole.UserRole)["mtime"])
        for c in children:
            c.setCheckState(0, Qt.CheckState.Unchecked if c is target else Qt.CheckState.Checked)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        self._check_guard()

    def _check_guard(self) -> None:
        """防呆:不允許整組全勾。"""
        any_full_group = False
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            children = [top.child(j) for j in range(top.childCount()) if top.child(j).data(0, Qt.ItemDataRole.UserRole)]
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
        item = items[0]
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            self._preview_image.setText("")
            self._preview_meta.setText("")
            return

        path = data["path"]
        ext = os.path.splitext(path)[1].lower()
        if ext in analysis.IMAGE_EXTS:
            pix = QPixmap(path)
            if pix.isNull():
                self._preview_image.setText("無法預覽")
                self._preview_image.setPixmap(QPixmap())
            else:
                scaled = pix.scaled(320, 320, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self._preview_image.setPixmap(scaled)
                self._preview_image.setText("")
        else:
            self._preview_image.setPixmap(QPixmap())
            self._preview_image.setText("(不支援預覽)" if ext in analysis.VIDEO_EXTS else "無法預覽")

        self._preview_meta.setText(
            f"{os.path.basename(path)}\n大小:{format_size(data['size'])}\n"
            f"修改日期:{analysis.format_relative_time(data['mtime'])}\n{wrap_path_for_label(path)}"
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
        if not confirm_delete(self, "刪除重複檔案", msg, huge_file=huge):
            return

        failures = []
        for child, data in entries:
            ok, message = safe_trash_delete(data["path"], data["size"])
            if ok:
                parent = child.parent()
                parent.removeChild(child)
            else:
                failures.append(f"{data['path']}: {message}")

        # 清掉不再有重複的組(剩不到 2 個檔案的組已無意義)
        for i in reversed(range(self._tree.topLevelItemCount())):
            top = self._tree.topLevelItem(i)
            remaining = [top.child(j) for j in range(top.childCount()) if top.child(j).data(0, Qt.ItemDataRole.UserRole)]
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
        elif action == open_action:
            if os.path.exists(data["path"]):
                subprocess.Popen(["explorer", "/select,", data["path"]])
