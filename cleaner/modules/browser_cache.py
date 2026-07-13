import glob
import os

from ..state import CleanResult, ScanResult
from .base import CleanerModule, ProgressThrottle, delete_entries, scan_directory

_GLOB_PATTERNS = [
    r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cache\Cache_Data",
    r"%LOCALAPPDATA%\Google\Chrome\User Data\Profile *\Cache\Cache_Data",
    r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Code Cache",
    r"%LOCALAPPDATA%\Google\Chrome\User Data\Profile *\Code Cache",
    r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\GPUCache",
    r"%LOCALAPPDATA%\Google\Chrome\User Data\Profile *\GPUCache",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cache\Cache_Data",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Profile *\Cache\Cache_Data",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Code Cache",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Profile *\Code Cache",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\GPUCache",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Profile *\GPUCache",
    r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles\*\cache2",
]


def _resolve_roots() -> list[str]:
    roots: list[str] = []
    for pattern in _GLOB_PATTERNS:
        expanded = os.path.expandvars(pattern)
        for path in glob.glob(expanded):
            if os.path.isdir(path):
                roots.append(path)
    return roots


class BrowserCacheModule(CleanerModule):
    module_id = "browser_cache"
    display_name = "瀏覽器快取"
    description = "Chrome / Edge / Firefox 的快取檔案(不含 Cookies、瀏覽紀錄、密碼)"
    requires_admin = False
    min_age_hours = 0

    def __init__(self):
        self.allowed_roots = _resolve_roots()

    def scan(self, progress_cb) -> ScanResult:
        throttle = ProgressThrottle(self.module_id, progress_cb)
        entries = []
        locked_count = 0
        error_count = 0
        for root in self.allowed_roots:
            root_entries, root_locked, root_error = scan_directory(root, self.min_age_hours, throttle)
            entries.extend(root_entries)
            locked_count += root_locked
            error_count += root_error
        total_size = sum(e.size for e in entries)
        return ScanResult(
            module_id=self.module_id,
            entries=entries,
            total_size=total_size,
            locked_count=locked_count,
            error_count=error_count,
        )

    def clean(self, result: ScanResult, progress_cb) -> CleanResult:
        throttle = ProgressThrottle(self.module_id, progress_cb)
        freed_bytes, deleted_count, skipped_count, errors, log_lines = delete_entries(
            result.entries, self.allowed_roots, throttle
        )
        clean_result = CleanResult(
            module_id=self.module_id,
            freed_bytes=freed_bytes,
            deleted_count=deleted_count,
            skipped_count=skipped_count,
            errors=errors,
        )
        clean_result.log_lines = log_lines
        return clean_result
