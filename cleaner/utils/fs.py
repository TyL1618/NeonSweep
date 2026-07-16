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


def wrap_path_for_label(path: str) -> str:
    """給會換行的多行 QLabel(如預覽面板的中繼資料)顯示路徑用:在每個 "/" 後面插入零寬空白
    (U+200B),讓 Qt 的自動換行找得到斷點。

    路徑本身沒有空白給 QLabel 的 word-wrap 判斷斷詞,所以整條路徑會被當成一個不可斷的
    「單字」,連帶把容器撐寬到整條路徑那麼寬、怎麼拖曳分隔線都縮不小(dupe_page/
    similarity_page 的預覽面板都出現過這個症狀)。零寬空白肉眼看不到、不影響顯示內容,
    只是給斷行演算法一個可以換行的位置。**只用在顯示文字上**——複製路徑、開啟檔案這類操作
    一律要用原始未插入零寬空白的路徑字串。
    """
    return display_path(path).replace("/", "/​")


def format_size(n: int) -> str:
    """以 1024 為底,輸出 B / KB / MB / GB / TB,保留兩位小數。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def long_path(p: str) -> str:
    p = os.path.abspath(p)
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


def is_reparse_point(entry: os.DirEntry) -> bool:
    try:
        st = entry.stat(follow_symlinks=False)
        return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True   # 讀不到就當作危險,跳過


def split_excludes(exclude_dirs: list[str] | None) -> tuple[list[str], set[str]]:
    """把排除清單拆成 (路徑前綴清單, 純目錄名集合),皆正規化(normcase)。

    含路徑分隔符的項目(如 ``C:\\Windows\\WinSxS``)當「路徑前綴」比對;不含分隔符的
    項目(如 ``$Recycle.Bin``、``System Volume Information``)當「目錄名」比對。這樣避免
    了舊版無錨點子字串比對的過度命中——例如使用者自建的 ``D:\\System Volume Information 備份``
    以前會被整個跳過,現在只有目錄名剛好等於排除名的才算。
    """
    prefixes: list[str] = []
    names: set[str] = set()
    for d in exclude_dirs or []:
        nd = os.path.normcase(d)
        if os.sep in nd or (os.altsep and os.altsep in nd):
            prefixes.append(nd.rstrip(os.sep + (os.altsep or "")))
        else:
            names.add(nd)
    return prefixes, names


def is_excluded_dir(entry_path: str, entry_name: str, prefixes: list[str], names: set[str]) -> bool:
    """給定目錄的完整路徑與 basename(呼叫端負責正規化前先傳原始值),判斷是否命中排除。
    目錄名精確比對 names;完整路徑對每個 prefix 做「相等或以 prefix + 分隔符為開頭」比對。
    """
    if os.path.normcase(entry_name) in names:
        return True
    if prefixes:
        norm = os.path.normcase(entry_path)
        for p in prefixes:
            if norm == p or norm.startswith(p + os.sep):
                return True
    return False


def safe_walk(root: str, on_error=None, exclude_dirs: list[str] | None = None):
    """唯一允許的遍歷器:不進入 reparse point,權限錯誤跳過。

    exclude_dirs:選填,命中即不下探該目錄(見 split_excludes 的分段比對規則),
    供分析功能排除 WinSxS、$Recycle.Bin 等雜訊目錄用(見 DEVDOC §8.1)。
    """
    prefixes, names = split_excludes(exclude_dirs)
    have_exclude = bool(prefixes or names)
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
                            if have_exclude and is_excluded_dir(entry.path, entry.name, prefixes, names):
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
