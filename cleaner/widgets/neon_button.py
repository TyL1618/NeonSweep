from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QPushButton

from .. import theme

RING_WIDTH = 3


class NeonButton(QPushButton):
    """圓形霓虹掃描按鈕:黑底、3px 粉藍漸層圓環描邊,中央文字,IDLE 時呼吸光暈。"""

    def __init__(self, text: str = "掃描", diameter: int = 180, parent=None):
        super().__init__(text, parent)
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

        self._glow = theme.make_glow(theme.NEON_PINK, radius=20)
        self.setGraphicsEffect(self._glow)

        self._anim = QPropertyAnimation(self._glow, b"blurRadius")
        self._anim.setDuration(2000)
        self._anim.setKeyValueAt(0.0, 15)
        self._anim.setKeyValueAt(0.5, 40)
        self._anim.setKeyValueAt(1.0, 15)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim.setLoopCount(-1)

    def start_breathing(self) -> None:
        self._anim.start()

    def stop_breathing(self) -> None:
        self._anim.stop()
        self._glow.setBlurRadius(20)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(
            RING_WIDTH, RING_WIDTH,
            self._diameter - RING_WIDTH * 2,
            self._diameter - RING_WIDTH * 2,
        )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BG))
        painter.drawEllipse(rect)

        gradient = QLinearGradient(0, 0, self._diameter, 0)
        gradient.setColorAt(0, QColor(theme.NEON_PINK))
        gradient.setColorAt(1, QColor(theme.NEON_BLUE))
        pen = QPen()
        pen.setWidthF(RING_WIDTH)
        pen.setBrush(gradient)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)

        painter.setPen(QColor(theme.TEXT_MAIN if self.isEnabled() else theme.TEXT_DIM))
        painter.setFont(QFont("Microsoft JhengHei UI", 16, QFont.Weight.Bold))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
