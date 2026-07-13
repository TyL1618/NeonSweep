from PyQt6.QtWidgets import QFrame

from .. import theme


class GlowLine(QFrame):
    """裝飾用霓虹燈條:2px 寬/高,粉藍漸層背景 + glow。氛圍感的主要來源。

    用於:視窗頂部橫貫燈條、側邊欄與內容區分隔線(orientation="vertical")、報告頁數字下方。
    """

    def __init__(self, orientation: str = "horizontal", parent=None):
        super().__init__(parent)
        if orientation == "vertical":
            self.setFixedWidth(2)
        else:
            self.setFixedHeight(2)
        self.setStyleSheet(f"background: {theme.GRADIENT_PINK_BLUE}; border: none;")
        self.setGraphicsEffect(theme.make_glow(theme.NEON_PINK, radius=15))
