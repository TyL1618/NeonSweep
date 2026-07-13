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
from ..workers import DevSpaceWorker
from .common import ChipRow, confirm_delete, safe_trash_delete_dir

PATH_ELIDE_WIDTH = 480
COL_PROJECT, COL_KIND, COL_SIZE, COL_ACTIVITY, COL_ACTIONS = range(5)
HEADERS = ["專案路徑", "類型", "大小", "最後活動", "操作"]


class _NumericItem(QTableWidgetItem):
    def __init__(self, display: str, value: float):
        super().__init__(display)
        self._value = value

    def __lt__(self, other):
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


class DevSpacePage(QWidget):
    """開發空間掃描(DEVDOC §8.4):node_modules / venv / .tox / target(旁有 Cargo.toml)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None
        self._rows: list[dict] = []

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

        title = QLabel("開發空間掃描")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel("找出 node_modules / venv / .tox / target 等可重新產生的開發快取")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        sysdrive = os.environ.get("SystemDrive", "C:").upper() + "\\"
        drives = list_drives()
        self._drive_chips = ChipRow(items=[(d, d.rstrip("\\")) for d in drives], default_checked={sysdrive})
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

        self._progress_bar = NeonProgressBar()
        self._progress_bar.set_indeterminate(True)
        layout.addWidget(self._progress_bar)

        self._found_label = QLabel("已找到 0 個開發空間")
        self._found_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._found_label.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-family: Consolas;")
        layout.addWidget(self._found_label)

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

    def _start_scan(self) -> None:
        drives = self._drive_chips.checked_keys()
        if not drives:
            return

        self._found_count = 0
        self._found_label.setText("已找到 0 個開發空間")
        self._path_label.setText("")
        self._stack.setCurrentWidget(self._scanning_page)

        self._thread = QThread(self)
        self._worker = DevSpaceWorker(drives)
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

    def _on_progress(self, path: str) -> None:
        self._found_count += 1
        self._found_label.setText(f"已找到 {self._found_count} 個開發空間")
        metrics = QFontMetrics(self._path_label.font())
        self._path_label.setText(metrics.elidedText(path, Qt.TextElideMode.ElideMiddle, PATH_ELIDE_WIDTH))

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

        self._table = QTableWidget(0, len(HEADERS))
        self._table.setHorizontalHeaderLabels(HEADERS)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 不要對中間欄位用 Stretch resize mode(會跟其他 Interactive 欄位的拖曳 handle 錯位),
        # 改用明確欄寬。
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(COL_PROJECT, 380)
        self._table.setColumnWidth(COL_KIND, 110)
        self._table.setColumnWidth(COL_SIZE, 90)
        self._table.setColumnWidth(COL_ACTIVITY, 170)
        self._table.setColumnWidth(COL_ACTIONS, 170)
        layout.addWidget(self._table, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        rescan_btn = QPushButton("重新掃描")
        rescan_btn.clicked.connect(lambda: self._stack.setCurrentWidget(self._setup_page))
        bottom.addWidget(rescan_btn)
        layout.addLayout(bottom)

        return page

    def _on_scan_finished(self, results: list[dict]) -> None:
        self._thread = None
        self._worker = None
        self._rows = sorted(results, key=lambda r: -r["size"])
        self._populate_table()
        self._stack.setCurrentWidget(self._results_page)

    def _populate_table(self) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        total_size = 0

        for row in self._rows:
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(r, COL_PROJECT, QTableWidgetItem(display_path(row["project_path"])))
            self._table.setItem(r, COL_KIND, QTableWidgetItem(row["kind"]))
            self._table.setItem(r, COL_SIZE, _NumericItem(format_size(row["size"]), row["size"]))
            total_size += row["size"]

            activity_text = analysis.format_relative_time(row["last_activity"])
            activity_item = _NumericItem(activity_text, row["last_activity"] or 0)
            if analysis.is_stale(row["last_activity"]):
                from PyQt6.QtGui import QColor

                activity_item.setForeground(QColor(theme.NEON_PINK))
                activity_item.setText(activity_text + "(久未使用)")
            self._table.setItem(r, COL_ACTIVITY, activity_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(2, 0, 2, 0)
            actions_layout.setSpacing(4)
            btn_style = "padding: 2px 6px; font-size: 8pt;"
            open_btn = QPushButton("開啟位置")
            open_btn.setStyleSheet(btn_style)
            open_btn.clicked.connect(lambda _c, p=row["project_path"]: self._open_location(p))
            del_btn = QPushButton("刪除")
            del_btn.setStyleSheet(btn_style)
            del_btn.clicked.connect(lambda _c, rr=row: self._delete_row(rr))
            actions_layout.addWidget(open_btn)
            actions_layout.addWidget(del_btn)
            self._table.setCellWidget(r, COL_ACTIONS, actions)

        self._table.setSortingEnabled(True)
        self._table.sortItems(COL_SIZE, Qt.SortOrder.DescendingOrder)
        self._summary_label.setText(f"共 {len(self._rows)} 個開發空間,合計 {format_size(total_size)}")

    def _open_location(self, path: str) -> None:
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", path])

    def _delete_row(self, row_data: dict) -> None:
        # 不提供全選刪除,一次刪一個,防手滑(DEVDOC §8.4)。
        msg = (
            f"確定要刪除這個快取目錄嗎?\n\n{row_data['cache_path']}\n"
            f"大小:{format_size(row_data['size'])}\n\n"
            "重新安裝依賴即可復原(npm install / pip install)"
        )
        if not confirm_delete(self, "刪除開發快取", msg):
            return

        ok, message = safe_trash_delete_dir(row_data["cache_path"])
        if not ok:
            QMessageBox.warning(self, "刪除失敗", f"{row_data['cache_path']}\n{message}")
            return

        self._rows = [r for r in self._rows if r["cache_path"] != row_data["cache_path"]]
        self._populate_table()
