from PyQt6.QtCore import QThread, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import smart_health, theme
from ..widgets.glow_line import GlowLine
from ..workers import SmartHealthWorker

COL_DEVICE, COL_MODEL, COL_STATUS, COL_TEMP, COL_METRIC, COL_ACTIONS = range(6)
HEADERS = ["裝置", "型號", "健康狀態", "溫度", "關鍵指標", "操作"]


def _metric_text(health: dict) -> str:
    parts = []
    if health.get("wear_percent_used") is not None:
        parts.append(f"耗損 {health['wear_percent_used']}%")
    if health.get("reallocated_sectors"):
        parts.append(f"已重新對應磁區 {health['reallocated_sectors']}")
    if health.get("pending_sectors"):
        parts.append(f"待處理磁區 {health['pending_sectors']}")
    if health.get("uncorrectable"):
        parts.append(f"無法修正錯誤 {health['uncorrectable']}")
    return "、".join(parts) if parts else "—"


class HealthPage(QWidget):
    """磁碟健康(S.M.A.R.T.)診斷(不屬於 DEVDOC 原始規格,使用者後續要求新增)。

    只顯示、不提供任何修復功能——健康狀態異常時,使用者應自行備份資料並更換硬碟。
    底層透過第三方開源工具 smartmontools 的 smartctl.exe 讀取,見 DEVDOC §13。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None
        self._rows: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(14)

        title = QLabel("磁碟健康診斷")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel(
            "只顯示 S.M.A.R.T. 健康狀態,不提供任何修復功能。狀態異常時請自行備份資料並更換硬碟。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        self._missing_label = QLabel(
            "找不到 smartctl.exe。請自行從 smartmontools.org 下載 Windows 版本,"
            "解壓縮後把 smartctl.exe 與 drivedb.h 放到 third_party/smartmontools/ 資料夾"
            "(詳見 DEVDOC.md §13.1),再重新啟動本頁。"
        )
        self._missing_label.setWordWrap(True)
        self._missing_label.setStyleSheet(f"color: {theme.DANGER};")
        self._missing_label.setVisible(not smart_health.is_available())
        layout.addWidget(self._missing_label)

        button_row = QHBoxLayout()
        self._start_btn = QPushButton("開始診斷")
        self._start_btn.setEnabled(smart_health.is_available())
        self._start_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.GRADIENT_PINK_BLUE}; color: white; "
            f"border: none; border-radius: 6px; padding: 8px 24px; font-weight: bold; }}"
        )
        self._start_btn.clicked.connect(self._start_scan)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._cancel_scan)
        self._cancel_btn.setVisible(False)
        button_row.addWidget(self._start_btn)
        button_row.addWidget(self._cancel_btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        layout.addWidget(GlowLine())

        self._table = QTableWidget(0, len(HEADERS))
        self._table.setHorizontalHeaderLabels(HEADERS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(COL_DEVICE, 90)
        self._table.setColumnWidth(COL_MODEL, 260)
        self._table.setColumnWidth(COL_STATUS, 90)
        self._table.setColumnWidth(COL_TEMP, 70)
        self._table.setColumnWidth(COL_METRIC, 260)
        self._table.setColumnWidth(COL_ACTIONS, 100)
        layout.addWidget(self._table, 1)

    def _start_scan(self) -> None:
        self._rows = []
        self._table.setRowCount(0)
        self._start_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)

        self._thread = QThread(self)
        self._worker = SmartHealthWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._worker.device_found.connect(self._on_device_found)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _cancel_scan(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _on_device_found(self, health: dict) -> None:
        self._rows.append(health)
        r = self._table.rowCount()
        self._table.insertRow(r)
        self._table.setItem(r, COL_DEVICE, QTableWidgetItem(health["device"]))
        self._table.setItem(r, COL_MODEL, QTableWidgetItem(health.get("model") or health["device"]))

        passed = health.get("passed")
        status_item = QTableWidgetItem("良好" if passed else ("異常" if passed is False else "未知"))
        color = theme.OK if passed is True else (theme.DANGER if passed is False else theme.TEXT_DIM)
        status_item.setForeground(QColor(color))
        self._table.setItem(r, COL_STATUS, status_item)

        temp = health.get("temperature_c")
        self._table.setItem(r, COL_TEMP, QTableWidgetItem(f"{temp}°C" if temp is not None else "—"))
        self._table.setItem(r, COL_METRIC, QTableWidgetItem(_metric_text(health)))

        detail_btn = QPushButton("詳細報告")
        detail_btn.setStyleSheet("padding: 2px 6px; font-size: 8pt;")
        detail_btn.clicked.connect(lambda _c, h=health: self._show_detail(h))
        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(2, 0, 2, 0)
        actions_layout.addWidget(detail_btn)
        self._table.setCellWidget(r, COL_ACTIONS, actions)

    def _show_detail(self, health: dict) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            text = smart_health.query_health_text(health["device"], health.get("type"))
        finally:
            QApplication.restoreOverrideCursor()

        dialog = QDialog(self)
        dialog.setWindowTitle(f"詳細報告 — {health.get('model') or health['device']}")
        dialog.resize(700, 560)
        dialog_layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text or "(讀取失敗,可能需要以系統管理員身分重新啟動)")
        dialog_layout.addWidget(view)
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(dialog.accept)
        dialog_layout.addWidget(close_btn)
        dialog.exec()

    def _on_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._start_btn.setEnabled(smart_health.is_available())
        self._start_btn.setText("重新診斷")
        self._cancel_btn.setVisible(False)
        if not self._rows:
            self._missing_label.setText(
                "沒有偵測到任何磁碟健康資訊。可能需要以系統管理員身分重新啟動本程式,"
                "或該磁碟控制器/USB 橋接晶片不支援 S.M.A.R.T. 直讀。"
            )
            self._missing_label.setVisible(True)

    def shutdown(self) -> None:
        if self._worker:
            self._worker.cancel()
        if self._thread:
            self._thread.wait(2000)
