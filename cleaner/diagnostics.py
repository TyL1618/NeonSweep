"""唯讀系統空間診斷(不屬於 DEVDOC 原始規格,使用者後續要求新增)。

這裡列的都是「風險太高、這套工具不會去清」的系統空間佔用類別(WinSxS 元件存放區、
顯卡驅動殘留、休眠檔、分頁檔、System Restore 還原點)。此模組只負責讀取/估算大小,
**不提供任何刪除功能**——這是刻意的設計,呼應 DEVDOC §5「零自動刪除」與白名單制的
精神:凡是這套工具沒把握安全逐檔刪除的東西,寧可只顯示數字、附上建議的外部工具,也
不要冒險自己動手。
"""

import os
import re
import subprocess
import time

from .utils.fs import safe_walk

WINSXS_PATH = os.path.expandvars(r"%SystemRoot%\WinSxS")
DRIVERSTORE_PATH = os.path.expandvars(r"%SystemRoot%\System32\DriverStore\FileRepository")

PROGRESS_INTERVAL = 500
PROGRESS_TIME_INTERVAL = 0.1

_SIZE_WITH_PERCENT_RE = re.compile(r"([\d.]+)\s*(TB|GB|MB|KB|B)\s*\((\d+)%\)")
_UNIT_MULTIPLIERS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _sum_dir_size(root: str, progress_cb=None, cancel_check=None) -> tuple[int, int, bool]:
    """回傳 (total_bytes, file_count, complete)。complete=False 代表過程中有目錄/檔案
    因權限不足被跳過,數字僅供參考、可能低估。
    """
    if not os.path.isdir(root):
        return 0, 0, True

    total = 0
    count = 0
    complete = True
    last_emit = 0.0

    def on_error(_d):
        nonlocal complete
        complete = False

    for entry in safe_walk(root, on_error=on_error):
        if cancel_check and cancel_check():
            complete = False
            break
        try:
            total += entry.stat(follow_symlinks=False).st_size
            count += 1
        except OSError:
            complete = False
            continue
        now = time.monotonic()
        if progress_cb and (count % PROGRESS_INTERVAL == 0 or (now - last_emit) >= PROGRESS_TIME_INTERVAL):
            last_emit = now
            progress_cb(count, total)

    return total, count, complete


def winsxs_size(progress_cb=None, cancel_check=None) -> tuple[int, int, bool]:
    return _sum_dir_size(WINSXS_PATH, progress_cb, cancel_check)


def driverstore_size(progress_cb=None, cancel_check=None) -> tuple[int, int, bool]:
    return _sum_dir_size(DRIVERSTORE_PATH, progress_cb, cancel_check)


def hibernation_file_size() -> int | None:
    sysdrive = os.environ.get("SystemDrive", "C:")
    try:
        return os.path.getsize(f"{sysdrive}\\hiberfil.sys")
    except OSError:
        return None


def pagefile_size() -> int | None:
    sysdrive = os.environ.get("SystemDrive", "C:")
    try:
        return os.path.getsize(f"{sysdrive}\\pagefile.sys")
    except OSError:
        return None


def shadow_copy_used_size(drive: str | None = None) -> int | None:
    """透過 vssadmin 查詢 System Restore(磁碟區陰影複製)已用的儲存空間。
    通常需要管理員權限,失敗(含非管理員、vssadmin 不存在、逾時)一律回傳 None。

    輸出文字會隨 Windows 語系不同(中文/英文標籤不同),所以不比對標籤字串,
    改抓「數字+單位+百分比」這個固定格式、且一定是三行同格式數字中的第一行
    (Used,不論語系,vssadmin 固定先印 Used 再印 Allocated/Maximum)。
    """
    drive = drive or os.environ.get("SystemDrive", "C:")
    try:
        result = subprocess.run(
            ["vssadmin", "list", "shadowstorage", f"/for={drive}"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    match = _SIZE_WITH_PERCENT_RE.search(result.stdout)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    return int(value * _UNIT_MULTIPLIERS.get(unit, 1))
