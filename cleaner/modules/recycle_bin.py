import ctypes
from ctypes import wintypes

from ..state import CleanResult, ScanResult
from ..utils.fs import format_size
from .base import CleanerModule


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


def query_recycle_bin() -> tuple[int, int]:
    """回傳 (總 bytes, 項目數),涵蓋所有磁碟"""
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    return info.i64Size, info.i64NumItems


def empty_recycle_bin() -> int:
    SHERB_NOCONFIRMATION = 0x1
    SHERB_NOPROGRESSUI = 0x2
    SHERB_NOSOUND = 0x4
    return ctypes.windll.shell32.SHEmptyRecycleBinW(
        None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    )


class RecycleBinModule(CleanerModule):
    module_id = "recycle_bin"
    display_name = "資源回收桶"
    description = "清空所有磁碟的資源回收桶"
    requires_admin = False
    min_age_hours = 0
    is_api_module = True

    def scan(self, progress_cb) -> ScanResult:
        total_size, num_items = query_recycle_bin()
        return ScanResult(
            module_id=self.module_id,
            entries=[],
            total_size=max(total_size, 0),
            locked_count=0,
            error_count=0,
            is_api_module=True,
        )

    def clean(self, result: ScanResult, progress_cb) -> CleanResult:
        total_size, num_items = query_recycle_bin()
        if total_size <= 0:
            return CleanResult(module_id=self.module_id, freed_bytes=0, deleted_count=0, skipped_count=0)

        empty_recycle_bin()
        # 回傳值非 0 不視為錯誤(回收桶已空時 API 會回 0x8000FFFF)。
        # 清理前已查過大小,清理後再查一次確認是否清空。
        after_size, after_items = query_recycle_bin()
        freed = max(total_size - max(after_size, 0), 0)
        clean_result = CleanResult(
            module_id=self.module_id,
            freed_bytes=freed,
            deleted_count=num_items,
            skipped_count=0,
        )
        clean_result.log_lines = [f"刪除 {format_size(freed)} 資源回收桶(所有磁碟,共 {num_items} 個項目)"]
        return clean_result
