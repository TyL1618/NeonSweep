"""三個分析頁(bigfile/dupe/devspace)共用的小元件:磁碟/類型 toggle chip 列、
send2trash 安全刪除(含刪前重新驗證)、確認對話框。不屬於 DEVDOC §7.1 固定的 widgets/
清單(那四個是全域共用元件),這裡是 M4 分析頁專屬的小工具,故獨立放在 views/common.py。
"""

import os

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImageReader, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from send2trash import send2trash

from .. import theme
from ..utils.fs import display_path

HUGE_FILE_THRESHOLD = 10 * 1024**3  # 10 GB,見 DEVDOC §5 規則 7


def load_thumbnail(path: str, max_side: int = 320):
    """讀圖並縮成最長邊 max_side 的縮圖 QPixmap;失敗回傳 None。

    用 QImageReader.setScaledSize 在「解碼階段」就縮小(JPEG 走快速縮小解碼路徑),先讀標頭拿到
    原尺寸、只在需要時設定縮放,避免像 QPixmap(path) 那樣把整張大圖(可能上億像素、數百 MB)整個
    載進記憶體再 scaled——那會讓 GUI 執行緒卡頓、記憶體瞬間暴衝。QImageReader 吃 Unicode 路徑,
    非 ASCII 路徑不像 cv2.imread 會失敗。
    """
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()  # 只讀標頭、不解碼
    if size.isValid() and size.width() > 0 and size.height() > 0:
        w, h = size.width(), size.height()
        if w > max_side or h > max_side:
            scale = min(max_side / w, max_side / h)
            reader.setScaledSize(QSize(max(1, int(w * scale)), max(1, int(h * scale))))
    img = reader.read()
    if img.isNull():
        return None
    return QPixmap.fromImage(img)


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
            QPushButton:disabled {{
                border: 1px dashed {theme.BG_HOVER};
                color: {theme.BG_HOVER};
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


class FolderPicker(QWidget):
    """磁碟 chips 之外的「指定資料夾範圍」選擇器:一顆新增鈕開檔案對話框、一顆移除鈕、
    一個路徑清單。四個掃描頁(dupe/bigfile/treemap/similarity)共用,避免各自複製。

    selected_folders() 回傳使用者已加入的資料夾(原始反斜線路徑);清單為空代表沒有縮小
    範圍,由呼叫端自行決定改用磁碟 chips。
    """

    def __init__(self, parent=None, hint: str | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        if hint:
            lbl = QLabel(hint)
            lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ 新增資料夾")
        add_btn.clicked.connect(self._add)
        remove_btn = QPushButton("移除選取")
        remove_btn.clicked.connect(self._remove)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._list = QListWidget()
        self._list.setMaximumHeight(90)
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._list.setStyleSheet(
            f"background-color: {theme.BG_PANEL}; color: {theme.TEXT_MAIN}; "
            f"font-family: Consolas; font-size: 9pt; border: 1px solid {theme.TEXT_DIM};"
        )
        layout.addWidget(self._list)

    def _add(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "選擇要掃描的資料夾")
        if not folder:
            return
        folder = os.path.normpath(folder)
        if folder in self.selected_folders():
            return
        item = QListWidgetItem(display_path(folder))
        item.setData(Qt.ItemDataRole.UserRole, folder)
        item.setToolTip(folder)
        self._list.addItem(item)

    def _remove(self) -> None:
        for item in self._list.selectedItems():
            self._list.takeItem(self._list.row(item))

    def selected_folders(self) -> list[str]:
        return [self._list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self._list.count())]


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
