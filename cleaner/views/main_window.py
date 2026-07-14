import ctypes

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..state import AppState
from ..utils.fs import icon_path
from ..widgets.glow_line import GlowLine
from .bigfile_page import BigFilePage
from .clean_page import CleanPage
from .devspace_page import DevSpacePage
from .diagnostic_page import DiagnosticPage
from .dupe_page import DupePage
from .health_page import HealthPage

NAV_ITEMS = [
    ("clean", "⚡", "清理"),
    ("bigfile", "📦", "大檔案"),
    ("dupe", "⧉", "重複檔案"),
    ("devspace", "⌘", "開發空間"),
    ("diagnostic", "🩺", "系統診斷"),
    ("health", "💚", "磁碟健康"),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NeonSweep")
        self.setWindowIcon(QIcon(icon_path()))
        self.resize(1060, 700)
        self.setMinimumSize(960, 640)
        self._apply_dark_titlebar()

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(GlowLine())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root_layout.addLayout(body, 1)

        self._nav_buttons: dict[str, QToolButton] = {}
        sidebar = self._build_sidebar()
        body.addWidget(sidebar)
        body.addWidget(GlowLine(orientation="vertical"))

        self._stack = QStackedWidget()
        self._clean_page = CleanPage()
        self._bigfile_page = BigFilePage()
        self._dupe_page = DupePage()
        self._devspace_page = DevSpacePage()
        self._diagnostic_page = DiagnosticPage()
        self._health_page = HealthPage()

        self._pages = {
            "clean": self._clean_page,
            "bigfile": self._bigfile_page,
            "dupe": self._dupe_page,
            "devspace": self._devspace_page,
            "diagnostic": self._diagnostic_page,
            "health": self._health_page,
        }
        for page in self._pages.values():
            self._stack.addWidget(page)
        body.addWidget(self._stack, 1)

        self._clean_page.state_changed.connect(self._on_clean_state_changed)

        self._nav_buttons["clean"].setChecked(True)
        self._stack.setCurrentWidget(self._clean_page)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setFixedWidth(64)
        sidebar.setStyleSheet(f"background-color: {theme.BG_PANEL};")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        group = QButtonGroup(sidebar)
        group.setExclusive(True)

        for key, icon, label in NAV_ITEMS:
            btn = QToolButton()
            btn.setText(icon)
            btn.setToolTip(label)
            btn.setCheckable(True)
            btn.setFixedSize(64, 56)
            btn.setStyleSheet(f"""
                QToolButton {{
                    background: transparent;
                    border: none;
                    border-left: 3px solid transparent;
                    color: {theme.TEXT_DIM};
                    font-size: 20pt;
                }}
                QToolButton:hover {{
                    color: {theme.NEON_PINK_L};
                }}
                QToolButton:checked {{
                    color: {theme.NEON_PINK};
                    border-left: 3px solid {theme.NEON_PINK};
                    background-color: {theme.BG_HOVER};
                }}
            """)
            btn.clicked.connect(lambda checked, k=key: self._switch_page(k))
            group.addButton(btn)
            layout.addWidget(btn)
            self._nav_buttons[key] = btn

        layout.addStretch(1)
        return sidebar

    def _switch_page(self, key: str) -> None:
        self._stack.setCurrentWidget(self._pages[key])

    def _on_clean_state_changed(self, new_state) -> None:
        locked = new_state in (AppState.SCANNING, AppState.CLEANING)
        for btn in self._nav_buttons.values():
            btn.setEnabled(not locked)

    def _apply_dark_titlebar(self) -> None:
        try:
            hwnd = int(self.winId())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4
            )  # DWMWA_USE_IMMERSIVE_DARK_MODE
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        for page in self._pages.values():
            page.shutdown()
        super().closeEvent(event)
