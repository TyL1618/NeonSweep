from dataclasses import dataclass, field
from enum import Enum, auto


class AppState(Enum):
    IDLE = auto()       # 只有一顆掃描按鈕
    SCANNING = auto()   # 掃描中
    PREVIEW = auto()    # 顯示掃描結果,等使用者勾選確認
    CLEANING = auto()   # 清理中
    DONE = auto()       # 顯示成果報告


@dataclass
class FileEntry:
    path: str
    size: int           # bytes


@dataclass
class ScanResult:
    module_id: str
    entries: list[FileEntry] = field(default_factory=list)
    total_size: int = 0
    locked_count: int = 0      # 掃描時就已無法存取的數量
    error_count: int = 0
    is_api_module: bool = False  # True = 回收桶這種不列個別檔案的模組


@dataclass
class CleanResult:
    module_id: str
    freed_bytes: int = 0
    deleted_count: int = 0
    skipped_count: int = 0     # 使用中/無權限而跳過
    errors: list[str] = field(default_factory=list)  # 只留前 100 筆,避免爆記憶體
