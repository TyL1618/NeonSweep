import os
import stat
import time

from ..state import CleanResult, FileEntry, ScanResult
from ..utils.fs import format_size, long_path, safe_walk

PROGRESS_FILE_INTERVAL = 200
PROGRESS_TIME_INTERVAL = 0.1  # 100ms
MAX_ERRORS = 100


class ScanCancelled(Exception):
    """由 ProgressThrottle.tick() 偵測到使用者取消時丟出,scan_directory / delete_entries 接住後
    提前結束迴圈並回傳目前已收集的部分結果。CleanerModule.scan/clean 的固定介面不受影響——
    取消訊號是透過 progress_cb 上的 cancel_check 屬性夾帶進來的(見 workers.py 的 _make_cb)。
    """


class ProgressThrottle:
    """節流器:每個項目都檢查是否已取消,但每 200 個檔案或每 100ms 才呼叫一次
    progress_cb(module_id, current_path, count),避免 signal 洪水凍死 UI。
    """

    def __init__(self, module_id: str, progress_cb):
        self._module_id = module_id
        self._cb = progress_cb
        self._cancel_check = getattr(progress_cb, "cancel_check", None)
        self._count = 0
        self._last_time = 0.0

    def tick(self, current_path: str) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise ScanCancelled()
        self._count += 1
        now = time.monotonic()
        if self._count % PROGRESS_FILE_INTERVAL == 0 or (now - self._last_time) >= PROGRESS_TIME_INTERVAL:
            self._last_time = now
            if self._cb:
                self._cb(self._module_id, current_path, self._count)

    @property
    def count(self) -> int:
        return self._count


def _is_within(path: str, root: str) -> bool:
    """判斷 path 是否位於 root 之內(含 root 自身),不分大小寫(路徑守衛用)。"""
    norm_path = os.path.normcase(os.path.abspath(path))
    norm_root = os.path.normcase(os.path.abspath(root))
    if not norm_root.endswith(os.sep):
        norm_root += os.sep
    return norm_path.startswith(norm_root) or norm_path == norm_root.rstrip(os.sep)


def scan_directory(
    root: str, min_age_hours: int, throttle: ProgressThrottle
) -> tuple[list[FileEntry], int, int]:
    """掃描單一目錄樹(經 safe_walk),回傳 (entries, locked_count, error_count)。"""
    entries: list[FileEntry] = []
    locked_count = 0
    error_count = 0
    if not os.path.isdir(root):
        return entries, locked_count, error_count

    cutoff = time.time() - min_age_hours * 3600 if min_age_hours > 0 else None

    try:
        for entry in safe_walk(root):
            throttle.tick(entry.path)
            try:
                st = entry.stat(follow_symlinks=False)
            except PermissionError:
                locked_count += 1
                continue
            except OSError:
                error_count += 1
                continue
            if cutoff is not None and st.st_mtime > cutoff:
                continue
            entries.append(FileEntry(path=entry.path, size=st.st_size))
    except ScanCancelled:
        pass
    return entries, locked_count, error_count


def delete_entries(
    entries: list[FileEntry], allowed_roots: list[str], throttle: ProgressThrottle
) -> tuple[int, int, int, list[str], list[str]]:
    """共用刪除邏輯,回傳 (freed_bytes, deleted_count, skipped_count, errors, log_lines)。

    log_lines:每個檔案一行「刪除|跳過 <size> <path>」,供 report.py 寫入清理日誌。

    規則(不可省略,見 DEVDOC §4.1):
    0. 路徑守衛:刪除前驗證路徑落在 allowed_roots 之內,不符者一律不刪。
    1. 逐檔 try/except,絕不用 shutil.rmtree。
    2. 檔案處理完後由深到淺清空目錄,不刪模組根目錄本身。
    3. 唯讀檔案先 chmod 再刪。
    4. 長路徑加 \\\\?\\ 前綴。
    """
    freed_bytes = 0
    deleted_count = 0
    skipped_count = 0
    errors: list[str] = []
    log_lines: list[str] = []
    touched_dirs: set[str] = set()

    def record_error(msg: str) -> None:
        if len(errors) < MAX_ERRORS:
            errors.append(msg)

    try:
        for fe in entries:
            throttle.tick(fe.path)
            abs_path = os.path.abspath(fe.path)

            if not any(_is_within(abs_path, root) for root in allowed_roots):
                skipped_count += 1
                record_error(f"路徑守衛拒絕(不在允許範圍內): {fe.path}")
                log_lines.append(f"跳過 {format_size(fe.size)} {fe.path}")
                continue

            op_path = long_path(abs_path) if len(abs_path) > 250 else abs_path
            try:
                try:
                    os.chmod(op_path, stat.S_IWRITE)
                except OSError:
                    pass
                os.remove(op_path)
                freed_bytes += fe.size
                deleted_count += 1
                touched_dirs.add(os.path.dirname(abs_path))
                log_lines.append(f"刪除 {format_size(fe.size)} {fe.path}")
            except (PermissionError, OSError) as e:
                skipped_count += 1
                record_error(f"{fe.path}: {e}")
                log_lines.append(f"跳過 {format_size(fe.size)} {fe.path}")
    except ScanCancelled:
        pass

    for d in sorted(touched_dirs, key=len, reverse=True):
        if any(os.path.normcase(d) == os.path.normcase(os.path.abspath(root).rstrip(os.sep)) for root in allowed_roots):
            continue
        try:
            os.rmdir(long_path(d) if len(d) > 250 else d)
        except OSError:
            pass

    return freed_bytes, deleted_count, skipped_count, errors, log_lines


class CleanerModule:
    module_id: str = ""          # 唯一識別,如 "user_temp"
    display_name: str = ""       # UI 顯示,如 "使用者暫存檔"
    description: str = ""        # 一行說明,顯示在 UI 副標
    requires_admin: bool = False  # True 時 UI 顯示 🛡 標記,非管理員模式下停用該項
    min_age_hours: int = 24      # 只刪「最後修改時間超過 N 小時」的檔案;0 = 不過濾
    allowed_roots: list[str] = []
    is_api_module: bool = False

    def scan(self, progress_cb) -> ScanResult:
        raise NotImplementedError

    def clean(self, result: ScanResult, progress_cb) -> CleanResult:
        raise NotImplementedError
