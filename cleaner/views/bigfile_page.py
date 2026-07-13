import os
import subprocess

from PyQt6.QtCore import QThread, Qt
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import analysis, theme
from ..utils.fs import display_path, format_size, list_drives
from ..widgets.neon_progress import NeonProgressBar
from ..workers import BigFileWorker
from .common import HUGE_FILE_THRESHOLD, ChipRow, confirm_delete, safe_trash_delete

PATH_ELIDE_WIDTH = 480

COL_CHECK, COL_SIZE, COL_CATEGORY, COL_ROLE, COL_NAME, COL_PATH, COL_ATIME, COL_MTIME, COL_ACTIONS = range(9)
HEADERS = ["", "大小", "類型", "用途", "檔名", "完整路徑", "最後存取", "最後修改", "操作"]


class _NumericItem(QTableWidgetItem):
    def __init__(self, display: str, value: float):
        super().__init__(display)
        self._value = value

    def __lt__(self, other):
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


class BigFilePage(QWidget):
    """大檔案掃描器(DEVDOC §8.1):top-200 heap + 用途分類 + atime 可靠性偵測。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None
        self._rows: list[dict] = []  # {size, path, mtime, atime, category, role, reliable}

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
        layout.setSpacing(16)

        title = QLabel("大檔案掃描器")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel("列出最大的檔案,標示用途與最後使用時間,供人工評估是否刪除")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        sysdrive = os.environ.get("SystemDrive", "C:").upper() + "\\"
        drives = list_drives()
        self._drive_chips = ChipRow(
            items=[(d, d.rstrip("\\")) for d in drives],
            default_checked={sysdrive},
        )
        layout.addWidget(self._drive_chips)

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

        self._scan_progress_bar = NeonProgressBar()
        self._scan_progress_bar.set_indeterminate(True)
        layout.addWidget(self._scan_progress_bar)

        self._scan_count_label = QLabel("已掃描 0 個檔案")
        self._scan_count_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._scan_count_label.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-family: Consolas;")
        layout.addWidget(self._scan_count_label)

        self._scan_path_label = QLabel("")
        self._scan_path_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._scan_path_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas; font-size: 9pt;")
        layout.addWidget(self._scan_path_label)

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self._cancel_scan)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(cancel_btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

        return page

    def _start_scan(self) -> None:
        drives = self._drive_chips.checked_keys()
        if not drives:
            return

        self._scan_count_label.setText("已掃描 0 個檔案")
        self._scan_path_label.setText("")
        self._stack.setCurrentWidget(self._scanning_page)

        self._thread = QThread(self)
        self._worker = BigFileWorker(drives)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_scan_finished)
        self._scanned_drives = list(drives)
        self._thread.start()

    def _cancel_scan(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _on_progress(self, count: int, path: str) -> None:
        self._scan_count_label.setText(f"已掃描 {count} 個檔案")
        metrics = QFontMetrics(self._scan_path_label.font())
        self._scan_path_label.setText(metrics.elidedText(path, Qt.TextElideMode.ElideMiddle, PATH_ELIDE_WIDTH))

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

        self._atime_banner = QLabel("")
        self._atime_banner.setStyleSheet(f"color: {theme.DANGER}; font-size: 9pt;")
        self._atime_banner.setWordWrap(True)
        self._atime_banner.setVisible(False)
        layout.addWidget(self._atime_banner)

        self._filter_chips = ChipRow(items=analysis.FILTER_LABELS, default_checked={"all"}, exclusive=True)
        self._filter_chips.selection_changed.connect(self._apply_filter)
        layout.addWidget(self._filter_chips)

        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 9pt;")
        self._stats_label.setWordWrap(True)
        layout.addWidget(self._stats_label)

        self._table = QTableWidget(0, len(HEADERS))
        self._table.setHorizontalHeaderLabels(HEADERS)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 注意:不要對中間欄位用 Stretch resize mode——它會跟其餘 Interactive 欄位的
        # 拖曳 handle 錯位(Stretch 欄位之後的欄位寬度計算會整個往右偏移)。改用明確欄寬,
        # 表格內容過寬時交給水平捲軸處理即可。
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(COL_CHECK, 28)
        self._table.setColumnWidth(COL_SIZE, 90)
        self._table.setColumnWidth(COL_CATEGORY, 70)
        self._table.setColumnWidth(COL_ROLE, 120)
        self._table.setColumnWidth(COL_NAME, 160)
        self._table.setColumnWidth(COL_PATH, 340)
        self._table.setColumnWidth(COL_ATIME, 150)
        self._table.setColumnWidth(COL_MTIME, 150)
        self._table.setColumnWidth(COL_ACTIONS, 170)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._table, 1)

        bottom = QFrame()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        self._selected_label = QLabel("")
        self._selected_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        bottom_layout.addWidget(self._selected_label)
        bottom_layout.addStretch(1)

        rescan_btn = QPushButton("重新掃描")
        rescan_btn.clicked.connect(lambda: self._stack.setCurrentWidget(self._setup_page))
        bottom_layout.addWidget(rescan_btn)

        self._delete_checked_btn = QPushButton("刪除勾選項目")
        self._delete_checked_btn.clicked.connect(self._delete_checked)
        bottom_layout.addWidget(self._delete_checked_btn)
        layout.addWidget(bottom)

        self._table.itemChanged.connect(self._on_item_changed)

        return page

    def _on_scan_finished(self, result: list[tuple]) -> None:
        cancelled = bool(self._worker and self._worker.cancelled)
        self._thread = None
        self._worker = None
        if cancelled:
            self._stack.setCurrentWidget(self._setup_page)
            return

        reliability = {d: analysis.atime_reliable(d) for d in getattr(self, "_scanned_drives", [])}
        self._rows = []
        for size, path, mtime, atime in result:
            category, role = analysis.classify(path)
            drive = path[:3].upper() if len(path) >= 3 else ""
            reliable = reliability.get(drive, True)
            self._rows.append(
                {
                    "size": size,
                    "path": path,
                    "mtime": mtime,
                    "atime": atime,
                    "category": category,
                    "role": role,
                    "reliable": reliable,
                }
            )

        unreliable_drives = [d for d, ok in reliability.items() if not ok]
        if unreliable_drives:
            names = "、".join(d.rstrip("\\") for d in unreliable_drives)
            self._atime_banner.setText(
                f"⚠ {names} 未啟用存取時間記錄,「最後存取」欄僅供參考,請搭配「最後修改」判斷"
            )
            self._atime_banner.setVisible(True)
        else:
            self._atime_banner.setVisible(False)

        self._filter_chips.chip("all").setChecked(True)
        self._populate_table()
        self._stack.setCurrentWidget(self._results_page)

    def _populate_table(self) -> None:
        self._table.setSortingEnabled(False)
        self._table.itemChanged.disconnect(self._on_item_changed)
        self._table.setRowCount(0)

        for row in self._rows:
            r = self._table.rowCount()
            self._table.insertRow(r)

            check_item = QTableWidgetItem()
            check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check_item.setCheckState(Qt.CheckState.Unchecked)
            self._table.setItem(r, COL_CHECK, check_item)

            self._table.setItem(r, COL_SIZE, _NumericItem(format_size(row["size"]), row["size"]))
            self._table.setItem(r, COL_CATEGORY, QTableWidgetItem(row["category"]))
            self._table.setItem(r, COL_ROLE, QTableWidgetItem(row["role"]))
            self._table.setItem(r, COL_NAME, QTableWidgetItem(os.path.basename(row["path"])))
            path_item = QTableWidgetItem(display_path(row["path"]))
            path_item.setData(Qt.ItemDataRole.UserRole, row["path"])
            self._table.setItem(r, COL_PATH, path_item)

            atime_text = analysis.format_relative_time(row["atime"]) if row["atime"] else "—"
            atime_item = _NumericItem(atime_text, row["atime"] or 0)
            if not row["reliable"]:
                atime_item.setForeground(_qcolor(theme.TEXT_DIM))
            elif analysis.is_stale(row["atime"]):
                atime_item.setForeground(_qcolor(theme.NEON_PINK))
            self._table.setItem(r, COL_ATIME, atime_item)

            mtime_text = analysis.format_relative_time(row["mtime"])
            mtime_item = _NumericItem(mtime_text, row["mtime"] or 0)
            if analysis.is_stale(row["mtime"]):
                mtime_item.setForeground(_qcolor(theme.NEON_PINK))
            self._table.setItem(r, COL_MTIME, mtime_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(2, 0, 2, 0)
            actions_layout.setSpacing(4)
            btn_style = "padding: 2px 6px; font-size: 8pt;"
            open_btn = QPushButton("開啟位置")
            open_btn.setStyleSheet(btn_style)
            open_btn.clicked.connect(lambda _c, p=row["path"]: self._open_location(p))
            del_btn = QPushButton("刪除")
            del_btn.setStyleSheet(btn_style)
            del_btn.clicked.connect(lambda _c, rr=row: self._delete_row(rr))
            actions_layout.addWidget(open_btn)
            actions_layout.addWidget(del_btn)
            self._table.setCellWidget(r, COL_ACTIONS, actions)

        self._table.setSortingEnabled(True)
        self._table.sortItems(COL_SIZE, Qt.SortOrder.DescendingOrder)
        self._table.itemChanged.connect(self._on_item_changed)
        self._update_stats()
        self._apply_filter()
        self._update_selected_label()

    def _update_stats(self) -> None:
        totals: dict[str, tuple[int, int]] = {}
        for row in self._rows:
            count, size = totals.get(row["category"], (0, 0))
            totals[row["category"]] = (count + 1, size + row["size"])
        parts = [f"{cat}: {count} 檔 / {format_size(size)}" for cat, (count, size) in totals.items()]
        self._stats_label.setText("   ".join(parts) if parts else "沒有找到檔案")

    def _apply_filter(self) -> None:
        selected = self._filter_chips.checked_keys()
        key = selected[0] if selected else "all"
        allowed = analysis.FILTER_GROUPS.get(key)
        for r in range(self._table.rowCount()):
            category = self._table.item(r, COL_CATEGORY).text()
            hidden = allowed is not None and category not in allowed
            self._table.setRowHidden(r, hidden)

    def _row_by_path(self, path: str) -> int | None:
        for r in range(self._table.rowCount()):
            if self._table.item(r, COL_PATH).data(Qt.ItemDataRole.UserRole) == path:
                return r
        return None

    def _open_location(self, path: str) -> None:
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", path])

    def _delete_row(self, row_data: dict) -> None:
        huge = row_data["size"] > HUGE_FILE_THRESHOLD
        msg = f"確定要刪除這個檔案嗎?\n\n{row_data['path']}\n大小:{format_size(row_data['size'])}"
        if row_data["category"] == "AI 模型":
            msg += "\n\n模型檔可從 Civitai / HuggingFace 重新下載"
        if not confirm_delete(self, "刪除檔案", msg, huge_file=huge):
            return

        ok, message = safe_trash_delete(row_data["path"], row_data["size"])
        if not ok:
            QMessageBox.warning(self, "刪除失敗", f"{row_data['path']}\n{message}")
            return

        self._rows = [r for r in self._rows if r["path"] != row_data["path"]]
        idx = self._row_by_path(row_data["path"])
        if idx is not None:
            self._table.removeRow(idx)
        self._update_stats()
        self._update_selected_label()

    def _delete_checked(self) -> None:
        checked_paths = []
        total_size = 0
        for r in range(self._table.rowCount()):
            if self._table.item(r, COL_CHECK).checkState() == Qt.CheckState.Checked:
                path = self._table.item(r, COL_PATH).data(Qt.ItemDataRole.UserRole)
                row_data = next((x for x in self._rows if x["path"] == path), None)
                if row_data:
                    checked_paths.append(row_data)
                    total_size += row_data["size"]

        if not checked_paths:
            return

        huge = any(r["size"] > HUGE_FILE_THRESHOLD for r in checked_paths)
        msg = f"確定要刪除勾選的 {len(checked_paths)} 個檔案嗎?\n總計 {format_size(total_size)}"
        if not confirm_delete(self, "刪除勾選項目", msg, huge_file=huge):
            return

        failures = []
        for row_data in checked_paths:
            ok, message = safe_trash_delete(row_data["path"], row_data["size"])
            if ok:
                self._rows = [r for r in self._rows if r["path"] != row_data["path"]]
                idx = self._row_by_path(row_data["path"])
                if idx is not None:
                    self._table.removeRow(idx)
            else:
                failures.append(f"{row_data['path']}: {message}")

        self._update_stats()
        self._update_selected_label()
        if failures:
            QMessageBox.warning(self, "部分檔案未刪除", "\n".join(failures[:20]))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == COL_CHECK:
            self._update_selected_label()

    def _update_selected_label(self) -> None:
        count = 0
        total = 0
        for r in range(self._table.rowCount()):
            check_item = self._table.item(r, COL_CHECK)
            if check_item and check_item.checkState() == Qt.CheckState.Checked:
                path = self._table.item(r, COL_PATH).data(Qt.ItemDataRole.UserRole)
                row_data = next((x for x in self._rows if x["path"] == path), None)
                if row_data:
                    count += 1
                    total += row_data["size"]
        if count:
            self._selected_label.setText(f"已勾選 {count} 個檔案,共 {format_size(total)}")
            self._delete_checked_btn.setText(f"刪除勾選項目(共 {format_size(total)})")
        else:
            self._selected_label.setText("")
            self._delete_checked_btn.setText("刪除勾選項目")

    def _show_context_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        path = self._table.item(row, COL_PATH).data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        copy_action = menu.addAction("複製路徑")
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == copy_action:
            from PyQt6.QtWidgets import QApplication

            QApplication.clipboard().setText(path)


def _qcolor(hex_color: str):
    from PyQt6.QtGui import QColor

    return QColor(hex_color)
