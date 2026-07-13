from PyQt6.QtCore import QObject, pyqtSignal

from . import analysis
from .modules.base import ScanCancelled


class ScanWorker(QObject):
    module_started = pyqtSignal(str)                 # module_id
    module_finished = pyqtSignal(str, object)         # module_id, ScanResult
    progress = pyqtSignal(str, str, int)               # module_id, current_path, count
    finished = pyqtSignal()

    def __init__(self, modules: list):
        super().__init__()
        self._modules = modules
        self._cancelled = False

    def cancel(self):          # 由主執行緒呼叫,只設旗標
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _make_cb(self, module_id):
        def cb(mid, path, count):
            self.progress.emit(mid, path, count)
        # 夾帶取消檢查:CleanerModule.scan(self, progress_cb) 介面不變,
        # 但 base.py 的 ProgressThrottle 會讀取這個屬性,在內層迴圈的每個項目都檢查取消旗標。
        cb.cancel_check = lambda: self._cancelled
        return cb

    def run(self):
        for m in self._modules:
            if self._cancelled:
                break
            self.module_started.emit(m.module_id)
            try:
                result = m.scan(self._make_cb(m.module_id))
            except ScanCancelled:
                # 安全網:萬一某模組的自訂迴圈忘了自己接住 ScanCancelled,
                # 這裡確保 finished 訊號一定會發出,執行緒才能正常收尾。
                break
            self.module_finished.emit(m.module_id, result)
        self.finished.emit()


class CleanWorker(QObject):
    module_started = pyqtSignal(str)                 # module_id
    module_finished = pyqtSignal(str, object)         # module_id, CleanResult
    progress = pyqtSignal(str, str, int)               # module_id, current_path, count
    finished = pyqtSignal()

    def __init__(self, jobs: list):
        """jobs: list[tuple[CleanerModule, ScanResult]],僅含使用者於 PREVIEW 勾選的模組。"""
        super().__init__()
        self._jobs = jobs
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _make_cb(self, module_id):
        def cb(mid, path, count):
            self.progress.emit(mid, path, count)
        cb.cancel_check = lambda: self._cancelled
        return cb

    def run(self):
        for module, scan_result in self._jobs:
            if self._cancelled:
                break
            self.module_started.emit(module.module_id)
            try:
                result = module.clean(scan_result, self._make_cb(module.module_id))
            except ScanCancelled:
                break
            self.module_finished.emit(module.module_id, result)
        self.finished.emit()


class BigFileWorker(QObject):
    """§8.1 大檔案掃描:依序(非平行)掃描每顆磁碟,維護 top-N 最小堆。"""

    progress = pyqtSignal(int, str)   # count, current_path
    finished = pyqtSignal(list)       # list[(size, path, mtime, atime)]

    def __init__(self, drives: list[str], top_n: int = analysis.TOP_N_FILES):
        super().__init__()
        self._drives = drives
        self._top_n = top_n
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        result = analysis.scan_top_files(
            self._drives,
            progress_cb=lambda count, path: self.progress.emit(count, path),
            cancel_check=lambda: self._cancelled,
            top_n=self._top_n,
        )
        self.finished.emit(result)


class DupeWorker(QObject):
    """§8.3 重複檔案三階段漏斗。"""

    progress = pyqtSignal(int, int, int, str)   # phase, done, total, current_path
    finished = pyqtSignal(list)                 # list[list[str]]

    def __init__(self, drives: list[str], extensions: set | None, min_size: int):
        super().__init__()
        self._drives = drives
        self._extensions = extensions
        self._min_size = min_size
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        result = analysis.find_duplicates(
            self._drives,
            self._extensions,
            self._min_size,
            progress_cb=lambda phase, done, total, path: self.progress.emit(phase, done, total, path),
            cancel_check=lambda: self._cancelled,
        )
        self.finished.emit(result)


class DevSpaceWorker(QObject):
    """§8.4 開發空間掃描(node_modules / venv / .tox / target)。"""

    progress = pyqtSignal(str)   # 找到的快取目錄路徑
    finished = pyqtSignal(list)  # list[dict]

    def __init__(self, drives: list[str]):
        super().__init__()
        self._drives = drives
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        result = analysis.find_devspaces(
            self._drives,
            progress_cb=lambda path: self.progress.emit(path),
            cancel_check=lambda: self._cancelled,
        )
        self.finished.emit(result)
