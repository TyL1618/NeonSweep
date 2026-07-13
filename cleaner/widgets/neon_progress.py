from PyQt6.QtWidgets import QProgressBar

from .. import theme


class NeonProgressBar(QProgressBar):
    """漸層進度條:黑底、粉藍漸層 chunk,掛 glow。

    set_indeterminate(True) 切換為不定長度模式(掃描階段用的跑馬燈)。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextVisible(False)
        self.setFixedHeight(10)
        self.setRange(0, 100)
        self.setStyleSheet(f"""
            QProgressBar {{
                background-color: {theme.BG_PANEL};
                border: none;
                border-radius: 5px;
            }}
            QProgressBar::chunk {{
                border-radius: 5px;
                background: {theme.GRADIENT_PINK_BLUE};
            }}
        """)
        self.setGraphicsEffect(theme.make_glow(theme.NEON_PINK, radius=15))

    def set_indeterminate(self, on: bool) -> None:
        if on:
            self.setRange(0, 0)
        else:
            self.setRange(0, 100)
