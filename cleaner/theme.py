from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsDropShadowEffect

BG = "#000000"          # 主背景,純黑
BG_PANEL = "#0a0a0f"    # 卡片/面板底
BG_HOVER = "#12121a"
NEON_PINK = "#ff2e88"
NEON_PINK_L = "#ff6ec7"  # 亮粉(hover / 漸層端點)
NEON_BLUE = "#00e5ff"
NEON_BLUE_D = "#4da6ff"
TEXT_MAIN = "#e8e8f0"
TEXT_DIM = "#8a8a9a"
DANGER = "#ff3b5c"
OK = "#39ffb0"

GRADIENT_PINK_BLUE = (
    "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
    f"stop:0 {NEON_PINK}, stop:1 {NEON_BLUE})"
)


def make_glow(color: str, radius: int = 25) -> QGraphicsDropShadowEffect:
    """建立一個發光效果。注意:一個 effect 實例只能掛一個 widget,每個 widget 要 new 一個。"""
    eff = QGraphicsDropShadowEffect()
    eff.setBlurRadius(radius)
    eff.setColor(QColor(color))
    eff.setOffset(0, 0)
    return eff


GLOBAL_QSS = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT_MAIN};
    font-family: "Microsoft JhengHei UI";
    font-size: 10pt;
}}

QMainWindow {{
    background-color: {BG};
}}

QToolTip {{
    background-color: {BG_PANEL};
    color: {TEXT_MAIN};
    border: 1px solid {NEON_PINK};
    padding: 4px;
}}

QPushButton {{
    background-color: transparent;
    border: 1px solid {NEON_BLUE_D};
    border-radius: 6px;
    padding: 6px 16px;
    color: {TEXT_MAIN};
}}

QPushButton:hover {{
    background-color: {BG_HOVER};
    border: 1px solid {NEON_PINK};
}}

QPushButton:disabled {{
    border: 1px solid {TEXT_DIM};
    color: {TEXT_DIM};
}}

QPushButton:pressed {{
    background-color: {BG_PANEL};
}}

QCheckBox {{
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {NEON_BLUE_D};
    border-radius: 3px;
    background-color: {BG_PANEL};
}}

QCheckBox::indicator:checked {{
    background-color: {NEON_PINK};
    border: 1px solid {NEON_PINK_L};
}}

QCheckBox::indicator:disabled {{
    border: 1px solid {TEXT_DIM};
    background-color: {BG_PANEL};
}}

QTableWidget, QTreeWidget {{
    background-color: {BG};
    alternate-background-color: {BG_PANEL};
    gridline-color: #1a1a24;
    border: 1px solid #1a1a24;
    selection-background-color: rgba(255, 46, 136, 60);
    selection-color: {TEXT_MAIN};
}}

QTreeView::indicator, QTableView::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {NEON_BLUE_D};
    border-radius: 3px;
    background-color: {BG_PANEL};
}}

QTreeView::indicator:checked, QTableView::indicator:checked {{
    background-color: {NEON_PINK};
    border: 1px solid {NEON_PINK_L};
}}

QHeaderView::section {{
    background-color: {BG_PANEL};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid #1a1a24;
    padding: 4px;
}}

QScrollBar:vertical {{
    background: {BG};
    width: 10px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: #2a2a36;
    border-radius: 5px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background: {NEON_PINK};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background: {BG};
    height: 10px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background: #2a2a36;
    border-radius: 5px;
    min-width: 24px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {NEON_PINK};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QPlainTextEdit {{
    background-color: {BG};
    border: 1px solid #1a1a24;
    color: {OK};
    font-family: Consolas;
}}
"""
