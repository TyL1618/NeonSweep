import os

from ..state import CleanResult, ScanResult
from .base import CleanerModule, ProgressThrottle, delete_entries, scan_directory


class UserTempModule(CleanerModule):
    module_id = "user_temp"
    display_name = "使用者暫存檔"
    description = "使用者帳號下的暫存檔案(%TEMP%)"
    requires_admin = False
    min_age_hours = 24

    def __init__(self):
        self._root = os.path.expandvars(r"%LOCALAPPDATA%\Temp")
        self.allowed_roots = [self._root]

    def scan(self, progress_cb) -> ScanResult:
        throttle = ProgressThrottle(self.module_id, progress_cb)
        entries, locked_count, error_count = scan_directory(self._root, self.min_age_hours, throttle)
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
