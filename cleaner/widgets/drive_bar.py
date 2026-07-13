import shutil

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QWidget

from .. import theme
from ..utils.fs import format_size


class DriveBar(QWidget):
    """磁碟容量霓虹燈條:圓角膠囊,已用部分=粉藍漸層,剩餘部分=BG_PANEL。
    使用率 > 90% 時漸層改成 DANGER 單色。左標磁碟代號,右標「已用 X / Y」。
    """

    def __init__(self, drive_root: str, parent=None):
        super().__init__(parent)
        self.drive_root = drive_root

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._label_letter = QLabel(drive_root.rstrip("\\"))
        self._label_letter.setStyleSheet(
            f"color: {theme.TEXT_MAIN}; font-family: Consolas; font-weight: bold; font-size: 11pt;"
        )
        self._label_letter.setFixedWidth(30)

        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(14)
        self._bar.setGraphicsEffect(theme.make_glow(theme.NEON_PINK, radius=12))

        self._label_usage = QLabel()
        self._label_usage.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas;")

        layout.addWidget(self._label_letter)
        layout.addWidget(self._bar, 1)
        layout.addWidget(self._label_usage)

        self.refresh()

    def refresh(self) -> None:
        try:
            usage = shutil.disk_usage(self.drive_root)
        except OSError:
            self._label_usage.setText("無法讀取")
            return

        used_pct = int(usage.used / usage.total * 100) if usage.total else 0
        self._bar.setRange(0, 100)
        self._bar.setValue(used_pct)

        chunk_bg = theme.DANGER if used_pct > 90 else theme.GRADIENT_PINK_BLUE
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: {theme.BG_PANEL};
                border: none;
                border-radius: 7px;
            }}
            QProgressBar::chunk {{
                border-radius: 7px;
                background: {chunk_bg};
            }}
        """)
        self._label_usage.setText(f"已用 {format_size(usage.used)} / {format_size(usage.total)}")
