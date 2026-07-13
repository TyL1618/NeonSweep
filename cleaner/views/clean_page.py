import os
import shutil
import time

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QTextCharFormat
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..modules import ALL_MODULES
from ..report import write_log
from ..state import AppState, CleanResult, ScanResult
from ..utils.admin import is_admin, relaunch_as_admin
from ..utils.fs import display_path, format_size, list_drives
from ..widgets.drive_bar import DriveBar
from ..widgets.glow_line import GlowLine
from ..widgets.neon_button import NeonButton
from ..widgets.neon_progress import NeonProgressBar
from ..workers import CleanWorker, ScanWorker

MAX_DETAIL_ENTRIES = 500
PATH_ELIDE_WIDTH = 560
LOG_DISPLAY_LIMIT = 2000  # 與 QPlainTextEdit.setMaximumBlockCount 一致,避免萬行 insertText 卡住主執行緒


def _elide(label: QLabel, text: str) -> None:
    metrics = QFontMetrics(label.font())
    label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, PATH_ELIDE_WIDTH))


class CleanPage(QWidget):
    """主頁:三段式清理流程狀態機(IDLE -> SCANNING -> PREVIEW -> CLEANING -> DONE)。"""

    state_changed = pyqtSignal(object)  # AppState

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = AppState.IDLE
        self._scan_results: dict[str, ScanResult] = {}
        self._clean_results: dict[str, CleanResult] = {}
        self._checkboxes: dict[str, QCheckBox] = {}
        self._scan_status_labels: dict[str, QLabel] = {}
        self._scan_path_labels: dict[str, QLabel] = {}
        self._detail_widgets: dict[str, QTreeWidget] = {}
        self._detail_expanded: dict[str, bool] = {}
        self._clean_progress_counts: dict[str, int] = {}
        self._clean_progress_totals: dict[str, int] = {}
        self._log_path: str | None = None
        self._scan_thread = None
        self._scan_worker = None
        self._clean_thread = None
        self._clean_worker = None
        self._pre_clean_free = 0
        self._clean_start_time = 0.0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        outer.addWidget(self._stack)

        self._idle_page = self._build_idle_page()
        self._scanning_page = self._build_scanning_page()
        self._preview_page = self._build_preview_page()
        self._cleaning_page = self._build_cleaning_page()
        self._done_page = self._build_done_page()

        for page in (
            self._idle_page,
            self._scanning_page,
            self._preview_page,
            self._cleaning_page,
            self._done_page,
        ):
            self._stack.addWidget(page)

        self._stack.setCurrentWidget(self._idle_page)

    # ------------------------------------------------------------------
    # 狀態切換
    # ------------------------------------------------------------------

    @property
    def state(self) -> AppState:
        return self._state

    def _set_state(self, new_state: AppState) -> None:
        self._state = new_state
        page_map = {
            AppState.IDLE: self._idle_page,
            AppState.SCANNING: self._scanning_page,
            AppState.PREVIEW: self._preview_page,
            AppState.CLEANING: self._cleaning_page,
            AppState.DONE: self._done_page,
        }
        self._stack.setCurrentWidget(page_map[new_state])
        self.state_changed.emit(new_state)

    # ------------------------------------------------------------------
    # IDLE
    # ------------------------------------------------------------------

    def _build_idle_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(16)

        self._idle_drive_bars: list[DriveBar] = []
        drives_box = QVBoxLayout()
        drives_box.setSpacing(8)
        for drive in list_drives():
            bar = DriveBar(drive)
            self._idle_drive_bars.append(bar)
            drives_box.addWidget(bar)
        layout.addLayout(drives_box)
        layout.addWidget(GlowLine())

        layout.addStretch(1)

        center = QVBoxLayout()
        center.setSpacing(12)
        center.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._scan_button = NeonButton("掃描", diameter=180)
        self._scan_button.clicked.connect(self.start_scan)
        self._scan_button.start_breathing()
        center.addWidget(self._scan_button, alignment=Qt.AlignmentFlag.AlignHCenter)

        hint = QLabel("掃描系統垃圾,不會刪除任何檔案")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        center.addWidget(hint)

        layout.addLayout(center)
        layout.addStretch(1)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        self._admin_button = QPushButton("⛨ 以管理員身分重新啟動")
        self._admin_button.clicked.connect(relaunch_as_admin)
        self._admin_button.setVisible(not is_admin())
        bottom_row.addWidget(self._admin_button)
        layout.addLayout(bottom_row)

        return page

    def refresh_drive_bars(self) -> None:
        for bar in self._idle_drive_bars:
            bar.refresh()

    # ------------------------------------------------------------------
    # SCANNING
    # ------------------------------------------------------------------

    def _build_scanning_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(12)

        title = QLabel("掃描中…")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)

        list_area = QScrollArea()
        list_area.setWidgetResizable(True)
        list_container = QWidget()
        list_layout = QVBoxLayout(list_container)
        list_layout.setSpacing(6)

        for module in ALL_MODULES:
            row = QFrame()
            row.setStyleSheet(f"background-color: {theme.BG_PANEL}; border-radius: 6px;")
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(12, 8, 12, 8)

            top_row = QHBoxLayout()
            name_label = QLabel(module.display_name)
            name_label.setStyleSheet(f"color: {theme.TEXT_MAIN};")
            status_label = QLabel("等待中")
            status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
            top_row.addWidget(name_label)
            top_row.addStretch(1)
            top_row.addWidget(status_label)
            row_layout.addLayout(top_row)

            path_label = QLabel("")
            path_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas; font-size: 9pt;")
            row_layout.addWidget(path_label)

            self._scan_status_labels[module.module_id] = status_label
            self._scan_path_labels[module.module_id] = path_label
            list_layout.addWidget(row)

        list_layout.addStretch(1)
        list_area.setWidget(list_container)
        layout.addWidget(list_area, 1)

        bottom_row = QHBoxLayout()
        self._scan_progress_bar = NeonProgressBar()
        self._scan_progress_bar.set_indeterminate(True)
        bottom_row.addWidget(self._scan_progress_bar, 1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.cancel_scan)
        bottom_row.addWidget(cancel_btn)
        layout.addLayout(bottom_row)

        return page

    def start_scan(self) -> None:
        self._scan_button.stop_breathing()
        self._scan_results = {}
        for status_label in self._scan_status_labels.values():
            status_label.setText("等待中")
            status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        for path_label in self._scan_path_labels.values():
            path_label.setText("")
        self._scan_progress_bar.set_indeterminate(True)

        self._set_state(AppState.SCANNING)

        self._scan_thread = QThread(self)
        self._scan_worker = ScanWorker(ALL_MODULES)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_worker.finished.connect(self._scan_worker.deleteLater)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)
        self._scan_worker.module_started.connect(self._on_scan_module_started)
        self._scan_worker.module_finished.connect(self._on_scan_module_finished)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_thread.start()

    def cancel_scan(self) -> None:
        if self._scan_worker:
            self._scan_worker.cancel()

    def _on_scan_module_started(self, module_id: str) -> None:
        label = self._scan_status_labels.get(module_id)
        if label:
            label.setText("掃描中…")
            label.setStyleSheet(f"color: {theme.NEON_BLUE};")

    def _on_scan_progress(self, module_id: str, path: str, count: int) -> None:
        label = self._scan_path_labels.get(module_id)
        if label:
            _elide(label, path)

    def _on_scan_module_finished(self, module_id: str, result: ScanResult) -> None:
        self._scan_results[module_id] = result
        label = self._scan_status_labels.get(module_id)
        if label:
            label.setText(f"{format_size(result.total_size)} ✓")
            label.setStyleSheet(f"color: {theme.OK};")
        path_label = self._scan_path_labels.get(module_id)
        if path_label:
            path_label.setText("")

    def _on_scan_finished(self) -> None:
        cancelled = bool(self._scan_worker and self._scan_worker.cancelled)
        self._scan_thread = None
        self._scan_worker = None
        if self._state != AppState.SCANNING:
            return
        if cancelled:
            self._reset_to_idle()
            return
        self._populate_preview()
        self._set_state(AppState.PREVIEW)

    # ------------------------------------------------------------------
    # PREVIEW
    # ------------------------------------------------------------------

    def _build_preview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 16)
        layout.setSpacing(10)

        title = QLabel("掃描結果")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)

        list_area = QScrollArea()
        list_area.setWidgetResizable(True)
        self._preview_list_container = QWidget()
        self._preview_list_layout = QVBoxLayout(self._preview_list_container)
        self._preview_list_layout.setSpacing(8)
        list_area.setWidget(self._preview_list_container)
        layout.addWidget(list_area, 1)

        bottom = QFrame()
        bottom.setStyleSheet(f"background-color: {theme.BG_PANEL}; border-radius: 8px;")
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(16, 12, 16, 12)

        self._total_size_label = QLabel("總計可釋放 0.00 GB")
        self._total_size_label.setStyleSheet(
            f"color: {theme.NEON_PINK}; font-family: Consolas; font-size: 16pt; font-weight: bold;"
        )
        self._total_size_label.setGraphicsEffect(theme.make_glow(theme.NEON_PINK, radius=18))
        bottom_layout.addWidget(self._total_size_label)
        bottom_layout.addStretch(1)

        rescan_btn = QPushButton("重新掃描")
        rescan_btn.clicked.connect(self.start_scan)
        bottom_layout.addWidget(rescan_btn)

        self._clean_start_btn = QPushButton("開始清理")
        self._clean_start_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.GRADIENT_PINK_BLUE}; color: white; "
            f"border: none; border-radius: 6px; padding: 8px 24px; font-weight: bold; }}"
        )
        self._clean_start_btn.clicked.connect(self.start_clean)
        bottom_layout.addWidget(self._clean_start_btn)

        layout.addWidget(bottom)
        return page

    def _clear_preview_rows(self) -> None:
        while self._preview_list_layout.count():
            item = self._preview_list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._checkboxes.clear()
        self._detail_widgets.clear()
        self._detail_expanded.clear()

    def _populate_preview(self) -> None:
        self._clear_preview_rows()
        admin = is_admin()

        for module in ALL_MODULES:
            result = self._scan_results.get(module.module_id)
            if result is None:
                continue

            card = QFrame()
            card.setStyleSheet(f"background-color: {theme.BG_PANEL}; border-radius: 6px;")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)

            row = QHBoxLayout()
            checkbox = QCheckBox(module.display_name)
            enabled = True
            if result.total_size == 0:
                enabled = False
            elif module.requires_admin and not admin:
                enabled = False

            checkbox.setEnabled(enabled)
            checkbox.setChecked(enabled)
            checkbox.stateChanged.connect(self._update_total_size_label)
            self._checkboxes[module.module_id] = checkbox
            row.addWidget(checkbox)

            if module.requires_admin and not admin:
                shield = QLabel("🛡 需要管理員權限")
                shield.setStyleSheet(f"color: {theme.TEXT_DIM};")
                row.addWidget(shield)

            row.addStretch(1)
            size_label = QLabel(format_size(result.total_size))
            size_label.setStyleSheet(f"color: {theme.NEON_PINK}; font-family: Consolas;")
            row.addWidget(size_label)
            card_layout.addLayout(row)

            desc_label = QLabel(module.description)
            desc_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 9pt;")
            card_layout.addWidget(desc_label)

            if module.module_id == "browser_cache" and result.locked_count > 0:
                hint = QLabel("部分檔案使用中,關閉瀏覽器可清得更乾淨")
                hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 9pt;")
                card_layout.addWidget(hint)

            if not result.is_api_module and result.entries:
                toggle_btn = QPushButton(f"▶ 檔案明細({len(result.entries)} 筆)")
                toggle_btn.setFlat(True)
                toggle_btn.setStyleSheet(f"QPushButton {{ color: {theme.NEON_BLUE_D}; border: none; text-align: left; }}")
                detail_tree = QTreeWidget()
                detail_tree.setHeaderLabels(["路徑", "大小"])
                detail_tree.setVisible(False)
                detail_tree.setFixedHeight(300)  # ~10 筆可見,不靠 sizeHint 決定高度
                detail_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
                detail_tree.setColumnWidth(1, 100)

                shown = result.entries[:MAX_DETAIL_ENTRIES]
                for entry in shown:
                    QTreeWidgetItem(detail_tree, [display_path(entry.path), format_size(entry.size)])
                remaining = len(result.entries) - len(shown)
                if remaining > 0:
                    QTreeWidgetItem(detail_tree, [f"…以及另外 {remaining} 個檔案", ""])

                def make_toggle(mid=module.module_id, btn=toggle_btn, tree=detail_tree):
                    def _toggle():
                        expanded = not self._detail_expanded.get(mid, False)
                        self._detail_expanded[mid] = expanded
                        tree.setVisible(expanded)
                        btn.setText(btn.text().replace("▶", "▼") if expanded else btn.text().replace("▼", "▶"))
                    return _toggle

                toggle_btn.clicked.connect(make_toggle())
                card_layout.addWidget(toggle_btn)
                card_layout.addWidget(detail_tree)
                self._detail_widgets[module.module_id] = detail_tree

            self._preview_list_layout.addWidget(card)

        self._preview_list_layout.addStretch(1)
        self._update_total_size_label()

    def _update_total_size_label(self) -> None:
        total = 0
        for module_id, checkbox in self._checkboxes.items():
            if checkbox.isChecked() and checkbox.isEnabled():
                result = self._scan_results.get(module_id)
                if result:
                    total += result.total_size
        self._total_size_label.setText(f"總計可釋放 {format_size(total)}")
        self._clean_start_btn.setEnabled(total > 0)

    # ------------------------------------------------------------------
    # CLEANING
    # ------------------------------------------------------------------

    def _build_cleaning_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(12)

        self._clean_progress_bar = NeonProgressBar()
        layout.addWidget(self._clean_progress_bar)

        info_frame = QFrame()
        info_layout = QVBoxLayout(info_frame)
        self._clean_module_label = QLabel("")
        self._clean_module_label.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-family: Consolas;")
        self._clean_path_label = QLabel("")
        self._clean_path_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas; font-size: 9pt;")

        stats_row = QHBoxLayout()
        self._clean_freed_label = QLabel("已釋放 0.00 GB")
        self._clean_freed_label.setStyleSheet(f"color: {theme.NEON_PINK}; font-family: Consolas;")
        self._clean_deleted_label = QLabel("已刪除 0 檔")
        self._clean_deleted_label.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-family: Consolas;")
        self._clean_skipped_label = QLabel("已跳過 0 檔")
        self._clean_skipped_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas;")
        stats_row.addWidget(self._clean_freed_label)
        stats_row.addWidget(self._clean_deleted_label)
        stats_row.addWidget(self._clean_skipped_label)
        stats_row.addStretch(1)

        info_layout.addWidget(self._clean_module_label)
        info_layout.addWidget(self._clean_path_label)
        info_layout.addLayout(stats_row)
        layout.addWidget(info_frame)

        self._clean_log = QPlainTextEdit()
        self._clean_log.setReadOnly(True)
        self._clean_log.setMaximumBlockCount(2000)
        layout.addWidget(self._clean_log, 1)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        self._clean_cancel_btn = QPushButton("取消")
        self._clean_cancel_btn.clicked.connect(self.cancel_clean)
        bottom_row.addWidget(self._clean_cancel_btn)
        layout.addLayout(bottom_row)

        return page

    def start_clean(self) -> None:
        jobs = []
        for module in ALL_MODULES:
            checkbox = self._checkboxes.get(module.module_id)
            if checkbox and checkbox.isChecked() and checkbox.isEnabled():
                result = self._scan_results.get(module.module_id)
                if result:
                    jobs.append((module, result))

        if not jobs:
            return

        self._clean_results = {}
        self._clean_progress_counts = {}
        self._clean_progress_totals = {
            module.module_id: (len(result.entries) if not result.is_api_module else 1)
            for module, result in jobs
        }
        self._clean_progress_bar.set_indeterminate(False)
        self._clean_progress_bar.setValue(0)
        self._clean_module_label.setText("")
        self._clean_path_label.setText("")
        self._clean_freed_label.setText("已釋放 0.00 GB")
        self._clean_deleted_label.setText("已刪除 0 檔")
        self._clean_skipped_label.setText("已跳過 0 檔")
        self._clean_log.clear()

        sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
        try:
            self._pre_clean_free = shutil.disk_usage(sysdrive).free
        except OSError:
            self._pre_clean_free = 0
        self._clean_start_time = time.monotonic()

        self._set_state(AppState.CLEANING)

        self._clean_thread = QThread(self)
        self._clean_worker = CleanWorker(jobs)
        self._clean_worker.moveToThread(self._clean_thread)
        self._clean_thread.started.connect(self._clean_worker.run)
        self._clean_worker.finished.connect(self._clean_thread.quit)
        self._clean_worker.finished.connect(self._clean_worker.deleteLater)
        self._clean_thread.finished.connect(self._clean_thread.deleteLater)
        self._clean_worker.module_started.connect(self._on_clean_module_started)
        self._clean_worker.module_finished.connect(self._on_clean_module_finished)
        self._clean_worker.progress.connect(self._on_clean_progress)
        self._clean_worker.finished.connect(self._on_clean_finished)
        self._clean_thread.start()

    def cancel_clean(self) -> None:
        if self._clean_worker:
            self._clean_worker.cancel()

    def _update_clean_progress_bar(self) -> None:
        total = sum(self._clean_progress_totals.values()) or 1
        done = sum(self._clean_progress_counts.values())
        pct = min(int(done / total * 100), 100)
        self._clean_progress_bar.setValue(pct)

    def _on_clean_module_started(self, module_id: str) -> None:
        module = next((m for m in ALL_MODULES if m.module_id == module_id), None)
        if module:
            self._clean_module_label.setText(module.display_name)

    def _on_clean_progress(self, module_id: str, path: str, count: int) -> None:
        self._clean_progress_counts[module_id] = count
        self._update_clean_progress_bar()
        _elide(self._clean_path_label, path)

    def _append_log_line(self, line: str) -> None:
        is_skip = line.startswith("跳過") or line.startswith("……")
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(theme.TEXT_DIM if is_skip else theme.OK))
        cursor = self._clean_log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.setCharFormat(fmt)
        cursor.insertText(line + "\n")
        self._clean_log.setTextCursor(cursor)
        self._clean_log.ensureCursorVisible()

    def _on_clean_module_finished(self, module_id: str, result: CleanResult) -> None:
        self._clean_results[module_id] = result
        total = self._clean_progress_totals.get(module_id, result.deleted_count + result.skipped_count)
        self._clean_progress_counts[module_id] = max(total, result.deleted_count + result.skipped_count)
        self._update_clean_progress_bar()

        freed_total = sum(r.freed_bytes for r in self._clean_results.values())
        deleted_total = sum(r.deleted_count for r in self._clean_results.values())
        skipped_total = sum(r.skipped_count for r in self._clean_results.values())
        self._clean_freed_label.setText(f"已釋放 {format_size(freed_total)}")
        self._clean_deleted_label.setText(f"已刪除 {deleted_total} 檔")
        self._clean_skipped_label.setText(f"已跳過 {skipped_total} 檔")

        log_lines = getattr(result, "log_lines", [])
        if len(log_lines) > LOG_DISPLAY_LIMIT:
            omitted = len(log_lines) - LOG_DISPLAY_LIMIT
            self._append_log_line(f"……(前 {omitted} 筆已略,完整記錄見日誌檔)")
            log_lines = log_lines[-LOG_DISPLAY_LIMIT:]
        for line in log_lines:
            self._append_log_line(line)

    def _on_clean_finished(self) -> None:
        self._clean_thread = None
        self._clean_worker = None
        if self._state != AppState.CLEANING:
            return
        elapsed = time.monotonic() - self._clean_start_time
        self._log_path = write_log(list(self._clean_results.values()), elapsed)
        self._populate_done(elapsed)
        self._set_state(AppState.DONE)

    # ------------------------------------------------------------------
    # DONE
    # ------------------------------------------------------------------

    def _build_done_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(12)
        layout.addStretch(1)

        self._done_freed_label = QLabel("已釋放 0.00 GB")
        self._done_freed_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._done_freed_label.setStyleSheet(
            f"color: {theme.NEON_PINK}; font-family: Consolas; font-size: 48pt; font-weight: bold;"
        )
        self._done_freed_label.setGraphicsEffect(theme.make_glow(theme.NEON_PINK, radius=30))
        layout.addWidget(self._done_freed_label)
        layout.addWidget(GlowLine())

        self._done_stats_label = QLabel("")
        self._done_stats_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._done_stats_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas;")
        layout.addWidget(self._done_stats_label)

        self._done_drive_label = QLabel("")
        self._done_drive_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._done_drive_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self._done_drive_label)

        layout.addStretch(1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        open_log_btn = QPushButton("開啟日誌")
        open_log_btn.clicked.connect(self._open_log)
        button_row.addWidget(open_log_btn)
        home_btn = QPushButton("回到首頁")
        home_btn.clicked.connect(self._reset_to_idle)
        button_row.addWidget(home_btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        return page

    def _populate_done(self, elapsed: float) -> None:
        freed_total = sum(r.freed_bytes for r in self._clean_results.values())
        deleted_total = sum(r.deleted_count for r in self._clean_results.values())
        skipped_total = sum(r.skipped_count for r in self._clean_results.values())

        self._done_freed_label.setText(f"已釋放 {format_size(freed_total)}")
        minutes, seconds = divmod(int(elapsed), 60)
        self._done_stats_label.setText(
            f"刪除 {deleted_total} 個檔案 · 跳過 {skipped_total} 個(使用中或無權限) · 耗時 {minutes} 分 {seconds} 秒"
        )

        self.refresh_drive_bars()
        sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
        try:
            after_free = shutil.disk_usage(sysdrive).free
            diff = after_free - self._pre_clean_free
            sign = "+" if diff >= 0 else "-"
            self._done_drive_label.setText(f"{sysdrive.rstrip(chr(92))} 剩餘空間 {sign}{format_size(abs(diff))}")
        except OSError:
            self._done_drive_label.setText("")

    def _open_log(self) -> None:
        if self._log_path and os.path.exists(self._log_path):
            os.startfile(self._log_path)

    def _reset_to_idle(self) -> None:
        self._scan_results = {}
        self._clean_results = {}
        self._scan_button.start_breathing()
        self.refresh_drive_bars()
        self._set_state(AppState.IDLE)

    # ------------------------------------------------------------------
    # 收尾(供 MainWindow closeEvent 呼叫)
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        if self._scan_worker:
            self._scan_worker.cancel()
        if self._scan_thread:
            self._scan_thread.wait(2000)
        if self._clean_worker:
            self._clean_worker.cancel()
        if self._clean_thread:
            self._clean_thread.wait(2000)
