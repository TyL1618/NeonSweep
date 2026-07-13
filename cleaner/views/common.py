"""三個分析頁(bigfile/dupe/devspace)共用的小元件:磁碟/類型 toggle chip 列、
send2trash 安全刪除(含刪前重新驗證)、確認對話框。不屬於 DEVDOC §7.1 固定的 widgets/
清單(那四個是全域共用元件),這裡是 M4 分析頁專屬的小工具,故獨立放在 views/common.py。
"""

import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QMessageBox, QPushButton, QWidget
from send2trash import send2trash

from .. import theme

HUGE_FILE_THRESHOLD = 10 * 1024**3  # 10 GB,見 DEVDOC §5 規則 7


class NeonChip(QPushButton):
    """圓角膠囊 toggle chip,用於磁碟選擇 / 類型篩選列。"""

    def __init__(self, key: str, label: str, parent=None):
        super().__init__(label, parent)
        self.key = key
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.BG_PANEL};
                border: 1px solid {theme.TEXT_DIM};
                border-radius: 12px;
                padding: 4px 14px;
                color: {theme.TEXT_DIM};
            }}
            QPushButton:checked {{
                border: 1px solid {theme.NEON_PINK};
                color: {theme.NEON_PINK};
                background-color: {theme.BG_HOVER};
            }}
            QPushButton:hover {{
                border: 1px solid {theme.NEON_PINK_L};
            }}
        """)


class ChipRow(QWidget):
    """一排 NeonChip。exclusive=True 時單選(篩選用),否則可複選(磁碟/類型選擇用)。"""

    selection_changed = pyqtSignal()

    def __init__(
        self,
        items: list[tuple[str, str]],
        default_checked: set[str] | None = None,
        exclusive: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._chips: dict[str, NeonChip] = {}
        self._group = QButtonGroup(self) if exclusive else None
        if self._group:
            self._group.setExclusive(True)

        default_checked = default_checked or set()
        for key, label in items:
            chip = NeonChip(key, label)
            chip.setChecked(key in default_checked)
            chip.toggled.connect(lambda _checked: self.selection_changed.emit())
            if self._group:
                self._group.addButton(chip)
            layout.addWidget(chip)
            self._chips[key] = chip

        layout.addStretch(1)

    def checked_keys(self) -> list[str]:
        return [k for k, chip in self._chips.items() if chip.isChecked()]

    def chip(self, key: str) -> NeonChip:
        return self._chips[key]


def confirm_delete(parent, title: str, message: str, huge_file: bool = False) -> bool:
    """§5 規則 5:分析功能刪除一律要確認對話框。huge_file=True 時額外標示 §5 規則 7 的警語。"""
    text = message
    if huge_file:
        text += "\n\n⚠ 此檔案可能超過回收桶容量上限,系統或將直接永久刪除"
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def safe_trash_delete(path: str, expected_size: int) -> tuple[bool, str]:
    """§5 規則 5+6:分析功能的刪除一律走 send2trash,刪除前重新 stat 驗證。

    回傳 (是否視為已處理, 訊息)。檔案已不存在視為成功(靜默跳過);
    大小與掃描時不符則拒刪並回報原因。
    """
    if not os.path.exists(path):
        return True, "檔案已不存在,略過"
    try:
        actual_size = os.path.getsize(path)
    except OSError as e:
        return False, str(e)
    if actual_size != expected_size:
        return False, "大小與掃描時不符(檔案已被修改),拒絕刪除"
    try:
        send2trash(path)
        return True, ""
    except Exception as e:
        return False, str(e)


def safe_trash_delete_dir(path: str) -> tuple[bool, str]:
    """開發空間刪除用:目標是整個快取目錄(node_modules 等)。目錄沒有單一「大小」可比對,
    只做存在性檢查(已不存在視為成功),一樣走 send2trash 可救回。
    """
    if not os.path.exists(path):
        return True, "目錄已不存在,略過"
    try:
        send2trash(path)
        return True, ""
    except Exception as e:
        return False, str(e)
