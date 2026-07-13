import os

from ..state import CleanResult, FileEntry, ScanResult
from .base import CleanerModule, ProgressThrottle, ScanCancelled, delete_entries, scan_directory


class CrashDumpsModule(CleanerModule):
    module_id = "crash_dumps"
    display_name = "錯誤報告與傾印檔"
    description = "當機傾印檔與 Windows 錯誤回報暫存"
    requires_admin = True
    min_age_hours = 0

    def __init__(self):
        self._dir_roots = [
            os.path.expandvars(r"%LOCALAPPDATA%\CrashDumps"),
            os.path.expandvars(r"%ProgramData%\Microsoft\Windows\WER\ReportQueue"),
            os.path.expandvars(r"%ProgramData%\Microsoft\Windows\WER\ReportArchive"),
            os.path.expandvars(r"%SystemRoot%\Minidump"),
        ]
        self._memory_dmp = os.path.expandvars(r"%SystemRoot%\MEMORY.DMP")
        self.allowed_roots = self._dir_roots + [self._memory_dmp]

    def scan(self, progress_cb) -> ScanResult:
        throttle = ProgressThrottle(self.module_id, progress_cb)
        entries: list[FileEntry] = []
        locked_count = 0
        error_count = 0

        for root in self._dir_roots:
            root_entries, root_locked, root_error = scan_directory(root, self.min_age_hours, throttle)
            entries.extend(root_entries)
            locked_count += root_locked
            error_count += root_error

        try:
            if os.path.isfile(self._memory_dmp):
                throttle.tick(self._memory_dmp)
                try:
                    size = os.path.getsize(self._memory_dmp)
                    entries.append(FileEntry(path=self._memory_dmp, size=size))
                except PermissionError:
                    locked_count += 1
                except OSError:
                    error_count += 1
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
