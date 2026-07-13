import os

from ..state import CleanResult, ScanResult
from .base import CleanerModule, ProgressThrottle, delete_entries, scan_directory

_CANDIDATE_ROOTS = [
    r"%LOCALAPPDATA%\pip\cache",
    r"%LOCALAPPDATA%\npm-cache",
    r"%LOCALAPPDATA%\Yarn\Cache",
    r"%LOCALAPPDATA%\NuGet\v3-cache",
]
# 注意:不要清 %USERPROFILE%\.nuget\packages,那是專案正在引用的套件本體,不是快取。


def _resolve_roots() -> list[str]:
    roots = []
    for pattern in _CANDIDATE_ROOTS:
        path = os.path.expandvars(pattern)
        if os.path.isdir(path):
            roots.append(path)
    return roots


class DevCachesModule(CleanerModule):
    module_id = "dev_caches"
    display_name = "開發者快取"
    description = "套件管理器的下載快取(pip / npm / yarn / NuGet),刪了只是重新下載"
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
