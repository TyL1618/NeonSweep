import os

from ..state import CleanResult, ScanResult
from .base import CleanerModule, ProgressThrottle, delete_entries, scan_directory

_CANDIDATE_ROOTS = [
    r"%USERPROFILE%\.cache\huggingface",
    r"%USERPROFILE%\.cache\torch",
    r"%USERPROFILE%\.insightface",
    r"%LOCALAPPDATA%\NVIDIA\ComputeCache",
]
# 注意:這些是模型/編譯結果的「下載或編譯快取」,刪了下次使用時該套件會自動重新下載/
# 重新編譯,不是唯一副本。但部分模型檔案可能單一就是好幾 GB,刪除前 PREVIEW 頁會照常
# 顯示大小讓使用者自行評估,不特別加額外警語。


def _resolve_roots() -> list[str]:
    roots = []
    for pattern in _CANDIDATE_ROOTS:
        path = os.path.expandvars(pattern)
        if os.path.isdir(path):
            roots.append(path)
    return roots


class AICachesModule(CleanerModule):
    module_id = "ai_caches"
    display_name = "AI 工具快取"
    description = "HuggingFace / PyTorch Hub / InsightFace 模型下載快取與 NVIDIA CUDA 編譯快取,刪了會在下次使用時重新下載或編譯"
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
