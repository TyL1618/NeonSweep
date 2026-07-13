import glob
import os

from ..state import CleanResult, FileEntry, ScanResult
from .base import CleanerModule, ProgressThrottle, ScanCancelled, delete_entries

_PATTERNS = ["thumbcache_*.db", "iconcache_*.db"]


class ThumbnailCacheModule(CleanerModule):
    module_id = "thumbnail_cache"
    display_name = "縮圖快取"
    description = "檔案總管的縮圖與圖示快取(通常被 Explorer 鎖住,刪不掉就跳過)"
    requires_admin = False
    min_age_hours = 0

    def __init__(self):
        self._root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Explorer")
        self.allowed_roots = [self._root]

    def scan(self, progress_cb) -> ScanResult:
        throttle = ProgressThrottle(self.module_id, progress_cb)
        entries: list[FileEntry] = []
        locked_count = 0
        error_count = 0

        try:
            for pattern in _PATTERNS:
                for path in glob.glob(os.path.join(self._root, pattern)):
                    throttle.tick(path)
                    try:
                        size = os.path.getsize(path)
                    except PermissionError:
                        locked_count += 1
                        continue
                    except OSError:
                        error_count += 1
                        continue
                    entries.append(FileEntry(path=path, size=size))
        except ScanCancelled:
            pass

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
