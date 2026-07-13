import datetime
import os

from .state import CleanResult
from .utils.fs import format_size

LOG_DIR = os.path.expandvars(r"%LOCALAPPDATA%\NeonSweep\logs")


def write_log(clean_results: list[CleanResult], elapsed_seconds: float) -> str:
    """寫入本次清理日誌,回傳日誌檔完整路徑。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"clean_{timestamp}.log")

    total_freed = 0
    total_deleted = 0
    total_skipped = 0

    with open(log_path, "w", encoding="utf-8") as f:
        for result in clean_results:
            f.write(f"== {result.module_id} ==\n")
            for line in getattr(result, "log_lines", []):
                f.write(line + "\n")
            for err in result.errors:
                f.write(f"錯誤: {err}\n")
            total_freed += result.freed_bytes
            total_deleted += result.deleted_count
            total_skipped += result.skipped_count

        f.write("\n== 總結 ==\n")
        f.write(f"釋放空間: {format_size(total_freed)}\n")
        f.write(f"刪除檔案數: {total_deleted}\n")
        f.write(f"跳過數: {total_skipped}\n")
        f.write(f"耗時: {elapsed_seconds:.1f} 秒\n")

    return log_path
