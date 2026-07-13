from PyQt6.QtCore import QThread, Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..utils.fs import format_size
from ..widgets.glow_line import GlowLine
from ..workers import DiagnosticWorker

# key, 顯示名稱, 說明(為什麼佔空間), 建議(這套工具不清,交給誰/怎麼處理)
CATEGORIES = [
    (
        "winsxs",
        "元件存放區(WinSxS)",
        "Windows 更新與驅動安裝後保留的舊版本系統元件,只增不減。",
        "建議:「磁碟清理」→「清理系統檔案」,或請系統管理員執行 DISM /Online /Cleanup-Image /StartComponentCleanup",
    ),
    (
        "driverstore",
        "顯示卡/裝置驅動殘留(DriverStore)",
        "每次更新驅動,舊版本預設不會自動清除,GPU 驅動更新頻繁的機器容易累積。",
        "建議:用驅動廠商的解安裝工具(如 NVIDIA 的 DDU)或裝置管理員手動移除舊版驅動",
    ),
    (
        "hiberfil",
        "休眠檔(hiberfil.sys)",
        "開啟休眠/快速啟動功能時佔用的固定空間,通常接近實體記憶體大小。",
        "建議:若不需要休眠功能,請系統管理員執行 powercfg /hibernate off",
    ),
    (
        "pagefile",
        "分頁檔(pagefile.sys)",
        "Windows 虛擬記憶體使用的空間,大小由系統或使用者設定管理。",
        "通常不需要清理,除非要手動調整虛擬記憶體大小上限",
    ),
    (
        "shadow_copy",
        "System Restore 還原點",
        "系統還原點與磁碟區陰影複製佔用的空間,會隨系統變動持續累積。",
        "建議:「控制台」→「系統」→「系統保護」→調整還原點磁碟空間上限,或刪除舊還原點",
    ),
]


class DiagnosticPage(QWidget):
    """唯讀系統空間診斷(不屬於 DEVDOC 原始規格)。只顯示數字,不提供任何刪除功能——
    這裡列的都是這套工具刻意不碰、風險太高的系統空間類別。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None
        self._size_labels: dict[str, QLabel] = {}
        self._status_labels: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(14)

        title = QLabel("系統空間診斷")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel(
            "只顯示、不刪除。這幾類系統空間風險太高,這套工具不會自動清,"
            "但先讓你知道「不是垃圾,是這些東西在佔」。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        button_row = QHBoxLayout()
        self._start_btn = QPushButton("開始診斷")
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

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self._card_layout = QVBoxLayout(container)
        self._card_layout.setSpacing(8)

        for key, name, why, advice in CATEGORIES:
            self._card_layout.addWidget(self._build_card(key, name, why, advice))
        self._card_layout.addStretch(1)

        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

    def _build_card(self, key: str, name: str, why: str, advice: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"background-color: {theme.BG_PANEL}; border-radius: 6px;")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)

        top_row = QHBoxLayout()
        name_label = QLabel(name)
        name_label.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-weight: bold;")
        status_label = QLabel("尚未診斷")
        status_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas;")
        top_row.addWidget(name_label)
        top_row.addStretch(1)
        top_row.addWidget(status_label)
        card_layout.addLayout(top_row)

        why_label = QLabel(why)
        why_label.setWordWrap(True)
        why_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 9pt;")
        card_layout.addWidget(why_label)

        advice_label = QLabel(advice)
        advice_label.setWordWrap(True)
        advice_label.setStyleSheet(f"color: {theme.NEON_BLUE_D}; font-size: 9pt;")
        card_layout.addWidget(advice_label)

        self._status_labels[key] = status_label
        return card

    def _start_scan(self) -> None:
        for status_label in self._status_labels.values():
            status_label.setText("等待中")
            status_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas;")

        self._start_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)

        self._thread = QThread(self)
        self._worker = DiagnosticWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._worker.category_started.connect(self._on_category_started)
        self._worker.progress.connect(self._on_progress)
        self._worker.category_finished.connect(self._on_category_finished)
        self._worker.finished.connect(self._on_all_finished)
        self._thread.start()

    def _cancel_scan(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _on_category_started(self, key: str) -> None:
        label = self._status_labels.get(key)
        if label:
            label.setText("查詢中…")
            label.setStyleSheet(f"color: {theme.NEON_BLUE}; font-family: Consolas;")

    def _on_progress(self, key: str, count: int, total_bytes: int) -> None:
        label = self._status_labels.get(key)
        if label:
            label.setText(f"查詢中…已掃描 {count} 個檔案,{format_size(total_bytes)}")

    def _on_category_finished(self, key: str, result: dict) -> None:
        label = self._status_labels.get(key)
        if not label:
            return
        size = result.get("size")
        if size is None:
            label.setText("無法讀取(可能需要管理員權限,或該功能未啟用)")
            label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas;")
            return

        text = format_size(size)
        if result.get("complete") is False:
            text += "(部分項目因權限被略過,僅供參考)"
        label.setText(text)
        label.setStyleSheet(f"color: {theme.NEON_PINK}; font-family: Consolas; font-weight: bold;")

    def _on_all_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._start_btn.setEnabled(True)
        self._start_btn.setText("重新診斷")
        self._cancel_btn.setVisible(False)

    def shutdown(self) -> None:
        if self._worker:
            self._worker.cancel()
        if self._thread:
            self._thread.wait(2000)
