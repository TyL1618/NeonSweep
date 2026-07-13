import ctypes
import os
import stat
import string
import sys


def app_root() -> str:
    """開發模式回傳專案根目錄;PyInstaller frozen 模式回傳解壓後的暫存根目錄
    (即 --add-data 打包進去的 datas 所在位置),供尋找 icon.ico 等隨附資源用。
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def icon_path() -> str:
    return os.path.join(app_root(), "icon.ico")


def list_drives() -> list[str]:
    """回傳存在且為固定磁碟的磁碟機根目錄,如 ['C:\\', 'D:\\']"""
    DRIVE_FIXED = 3
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED:
            drives.append(root)
    return drives


def display_path(path: str) -> str:
    """僅供 UI 顯示用:把反斜線換成斜線。

    Qt(至少在這個環境測得)對 "X:\\..." 這種正牌 Windows 磁碟路徑開頭的字串,
    在 QTableWidget/QTreeWidget 儲存格裡會觸發過度激進的省略號截斷(例如整條路徑被砍成
    "C:..."),不管欄寬設多少都一樣,換成正斜線後 Qt 的省略號計算就正常了。
    這只影響「顯示文字」,實際檔案操作一律要用未經轉換的原始路徑(反斜線)。
    """
    return path.replace("\\", "/")


def format_size(n: int) -> str:
    """以 1024 為底,輸出 B / KB / MB / GB,保留兩位小數。"""
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def long_path(p: str) -> str:
    p = os.path.abspath(p)
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


def is_reparse_point(entry: os.DirEntry) -> bool:
    try:
        st = entry.stat(follow_symlinks=False)
        return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True   # 讀不到就當作危險,跳過


def safe_walk(root: str, on_error=None, exclude_dirs: list[str] | None = None):
    """唯一允許的遍歷器:不進入 reparse point,權限錯誤跳過。

    exclude_dirs:選填,子字串比對(不分大小寫)命中即不下探該目錄,
    供分析功能排除 WinSxS、$Recycle.Bin 等雜訊目錄用(見 DEVDOC §8.1)。
    """
    exclude_lower = [os.path.normcase(d) for d in (exclude_dirs or [])]
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if is_reparse_point(entry):
                                continue
                            if exclude_lower:
                                norm = os.path.normcase(entry.path)
                                if any(ex in norm for ex in exclude_lower):
                                    continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            yield entry
                    except OSError:
                        continue
        except (PermissionError, OSError):
            if on_error:
                on_error(d)
            continue
