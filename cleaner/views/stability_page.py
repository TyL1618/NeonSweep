import os
import subprocess

from PyQt6.QtCore import QStandardPaths, QThread, QTimer, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import crash_analysis, crash_report, theme
from ..utils import admin
from ..widgets.glow_line import GlowLine
from ..widgets.neon_progress import NeonProgressBar
from ..workers import StabilityWorker

_SEV_COLOR = {"bad": theme.DANGER, "warn": theme.WARN, "info": theme.NEON_BLUE_D, "ok": theme.OK}
_SEV_ICON = {"bad": "🔴", "warn": "🟡", "info": "ℹ️", "ok": "✅"}
_TRANSPARENT = "background: transparent;"


def _label(text: str, style: str = "", wrap: bool = True, selectable: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(wrap)
    lab.setStyleSheet(_TRANSPARENT + style)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


def _card(border_color: str) -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    # 必須用 objectName 限定:QLabel 繼承自 QFrame,不限定的話 border-left 會套到每個子標籤上
    card.setObjectName("resultCard")
    card.setStyleSheet(
        f"QFrame#resultCard {{ background-color: {theme.BG_PANEL}; border-left: 4px solid {border_color}; "
        f"border-radius: 6px; }}"
    )
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 10, 14, 10)
    lay.setSpacing(4)
    return card, lay


class StabilityPage(QWidget):
    """電腦體檢與藍屏診斷:一鍵蒐集規格與事件記錄,顯示結論,並可匯出給別人看。

    這個頁面設計給「不懂電腦的人」用:打開就一顆按鈕,結果用紅黃綠 + 白話文字呈現,
    再用「複製摘要」或「匯出診斷包」把結果傳給幫忙的人。全程唯讀,不修改系統任何設定。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._worker = None
        self._data: dict | None = None
        self._analysis: dict | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(12)

        title = QLabel("電腦體檢與藍屏診斷")
        title.setStyleSheet(f"color: {theme.TEXT_MAIN}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(title)
        hint = QLabel(
            "一鍵查出電腦規格(CPU、記憶體、顯示卡、硬碟…)、近期藍屏與當機紀錄、硬碟健康度,"
            "並推測最可能的原因。全程只讀取,不會修改你的電腦。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)

        self._admin_banner = self._build_admin_banner()
        layout.addWidget(self._admin_banner)

        row = QHBoxLayout()
        self._start_btn = QPushButton("開始診斷")
        self._start_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.GRADIENT_PINK_BLUE}; color: white; "
            f"border: none; border-radius: 6px; padding: 10px 32px; font-weight: bold; font-size: 11pt; }}"
        )
        self._start_btn.clicked.connect(self._start)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._cancel)
        self._cancel_btn.setVisible(False)
        self._copy_btn = QPushButton("複製摘要")
        self._copy_btn.setToolTip("複製一段文字,可直接貼到 LINE / Messenger 給幫你的人")
        self._copy_btn.clicked.connect(self._copy_summary)
        self._export_btn = QPushButton("匯出診斷包")
        self._export_btn.setToolTip("產生一個 .zip 檔(含完整報告),傳給幫你的人")
        self._export_btn.clicked.connect(self._export)
        self._copy_btn.setEnabled(False)
        self._export_btn.setEnabled(False)
        row.addWidget(self._start_btn)
        row.addWidget(self._cancel_btn)
        row.addStretch(1)
        row.addWidget(self._copy_btn)
        row.addWidget(self._export_btn)
        layout.addLayout(row)

        self._dump_check = QCheckBox()
        self._dump_check.setVisible(False)
        layout.addWidget(self._dump_check)

        self._progress = NeonProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self._status)

        layout.addWidget(GlowLine())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._results = QWidget()
        self._results_layout = QVBoxLayout(self._results)
        self._results_layout.setContentsMargins(0, 0, 8, 0)
        self._results_layout.setSpacing(8)
        self._results_layout.addStretch(1)
        self._scroll.setWidget(self._results)
        layout.addWidget(self._scroll, 1)

    # ── 建構 ─────────────────────────────────────────────────────────────────

    def _build_admin_banner(self) -> QFrame:
        banner, lay = _card(theme.WARN)
        top = QHBoxLayout()
        top.addWidget(_label(
            "建議以「系統管理員」身分執行:否則讀不到藍屏傾印檔與部分硬碟健康資料,"
            "診斷會不完整(程式只讀取,不會改動任何東西)。", f"color: {theme.TEXT_MAIN};"), 1)
        btn = QPushButton("以系統管理員身分重新啟動")
        btn.clicked.connect(admin.relaunch_as_admin)
        top.addWidget(btn)
        lay.addLayout(top)
        banner.setVisible(not admin.is_admin())
        return banner

    def _clear_results(self) -> None:
        while self._results_layout.count() > 1:
            item = self._results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_result(self, widget: QWidget) -> None:
        self._results_layout.insertWidget(self._results_layout.count() - 1, widget)

    # ── 流程 ─────────────────────────────────────────────────────────────────

    def _start(self) -> None:
        self._clear_results()
        self._data = self._analysis = None
        self._copy_btn.setEnabled(False)
        self._export_btn.setEnabled(False)
        self._dump_check.setVisible(False)
        self._start_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)
        self._progress.setVisible(True)
        self._progress.set_indeterminate(False)
        self._progress.setValue(0)
        self._status.setText("準備中…")

        self._thread = QThread(self)
        self._worker = StabilityWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.result_ready.connect(self._on_result)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.finished.connect(self._on_finished)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
            self._status.setText("正在取消…")

    def _on_progress(self, label: str, index: int, total: int) -> None:
        self._status.setText(f"{label}…({index}/{total})")
        self._progress.setValue(int(index / max(total, 1) * 100))

    def _on_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._progress.setVisible(False)
        self._cancel_btn.setVisible(False)
        self._start_btn.setEnabled(True)
        self._start_btn.setText("重新診斷")
        if self._data is None:
            self._status.setText("已取消")

    def _on_result(self, data: dict, analysis: dict) -> None:
        self._data, self._analysis = data, analysis
        self._status.setText(f"診斷完成({data['collected_at']})")
        self._render(data, analysis)
        self._copy_btn.setEnabled(True)
        self._export_btn.setEnabled(True)
        dumps = [f for f in data.get("dumps", {}).get("files", []) if f.get("path")]
        if dumps:
            n = min(len(dumps), crash_report.MAX_DUMPS)
            mb = sum(f["size"] for f in dumps[:n]) / 1024 / 1024
            self._dump_check.setText(
                f"匯出時附上最近 {n} 個藍屏傾印檔(約 {mb:.1f} MB;可能含少量當時的記憶體內容,"
                f"能讓幫你的人更準確判斷,預設不附)")
            self._dump_check.setChecked(False)
            self._dump_check.setVisible(True)

    # ── 顯示結果 ─────────────────────────────────────────────────────────────

    def _render(self, data: dict, analysis: dict) -> None:
        overall = analysis["overall"]
        color = _SEV_COLOR[overall]
        verdict, vlay = _card(color)
        vlay.addWidget(_label(f"{_SEV_ICON[overall]}  {analysis['headline']}",
                              f"color: {color}; font-size: 13pt; font-weight: bold;"))
        if analysis["suspects"]:
            vlay.addWidget(_label("最可能的方向(依證據多寡排序):", f"color: {theme.TEXT_DIM};"))
            for i, s in enumerate(analysis["suspects"][:3], 1):
                vlay.addWidget(_label(f"{i}. {s['label']}", f"color: {theme.TEXT_MAIN}; font-weight: bold;"))
                vlay.addWidget(_label("　" + "；".join(s["reasons"]), f"color: {theme.TEXT_DIM};"))
        self._add_result(verdict)

        specs, slay = _card(theme.NEON_PINK)
        slay.addWidget(_label("電腦規格", f"color: {theme.TEXT_MAIN}; font-weight: bold;"))
        for k, v in crash_analysis.spec_rows(data):
            line = QHBoxLayout()
            key = _label(k, f"color: {theme.TEXT_DIM};", wrap=False)
            key.setFixedWidth(110)
            key.setAlignment(Qt.AlignmentFlag.AlignTop)
            line.addWidget(key)
            line.addWidget(_label(v, f"color: {theme.TEXT_MAIN};", selectable=True), 1)
            slay.addLayout(line)
        self._add_result(specs)

        for f in analysis["findings"]:
            card, lay = _card(_SEV_COLOR[f["severity"]])
            lay.addWidget(_label(f"{_SEV_ICON[f['severity']]}  {f['title']}",
                                 f"color: {theme.TEXT_MAIN}; font-weight: bold;"))
            if f["detail"]:
                lay.addWidget(_label(f["detail"], f"color: {theme.TEXT_DIM}; font-family: Consolas;",
                                     selectable=True))
            if f["advice"]:
                lay.addWidget(_label("建議:\n" + "\n".join(f"• {a}" for a in f["advice"]),
                                     f"color: {theme.TEXT_MAIN};"))
            self._add_result(card)

        if data.get("errors"):
            card, lay = _card(theme.TEXT_DIM)
            lay.addWidget(_label("以下項目讀不到(結果可能不完整):\n" + "\n".join(data["errors"]),
                                 f"color: {theme.TEXT_DIM};"))
            self._add_result(card)

    # ── 輸出 ─────────────────────────────────────────────────────────────────

    def _copy_summary(self) -> None:
        if not self._data:
            return
        QApplication.clipboard().setText(crash_report.build_summary_text(self._data, self._analysis))
        self._copy_btn.setText("已複製 ✓")
        QTimer.singleShot(2000, lambda: self._copy_btn.setText("複製摘要"))

    def _export(self) -> None:
        if not self._data:
            return
        include_dumps = self._dump_check.isVisible() and self._dump_check.isChecked()
        msg = ("診斷包會包含:\n• 電腦規格、診斷結論、事件紀錄摘要\n• 磁碟 SMART 報告\n\n"
               "使用者名稱、電腦名稱、IP、序號會自動遮蔽。")
        if include_dumps:
            msg += "\n\n⚠ 你勾選了附上傾印檔:這些是二進位檔,無法遮蔽,可能含少量當時的記憶體內容。"
        msg += "\n\n要繼續嗎?"
        if QMessageBox.question(self, "匯出診斷包", msg) != QMessageBox.StandardButton.Yes:
            return
        desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        path, _ = QFileDialog.getSaveFileName(
            self, "儲存診斷包", os.path.join(desktop, crash_report.default_bundle_name()),
            "診斷包 (*.zip)")
        if not path:
            return
        try:
            result = crash_report.export_bundle(path, self._data, self._analysis, include_dumps)
        except OSError as e:
            QMessageBox.warning(self, "匯出失敗", f"無法寫入檔案:{e}")
            return
        text = f"已儲存:\n{result['path']}\n大小 {result['size'] / 1024:.0f} KB\n\n請把這個檔案傳給幫你的人。"
        if result["dumps"]:
            text += f"\n\n已附上傾印檔:{', '.join(result['dumps'])}"
        for name, why in result["dumps_skipped"]:
            text += f"\n未附 {name}:{why}"
        box = QMessageBox(self)
        box.setWindowTitle("匯出完成")
        box.setText(text)
        open_btn = box.addButton("開啟所在資料夾", QMessageBox.ButtonRole.ActionRole)
        box.addButton("關閉", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def shutdown(self) -> None:
        if self._worker:
            self._worker.cancel()
        if self._thread:
            self._thread.wait(2000)
