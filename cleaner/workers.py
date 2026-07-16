from PyQt6.QtCore import QObject, pyqtSignal

from . import analysis, diagnostics, smart_health, treemap
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


class TreeSizeWorker(QObject):
    """空間視覺化:遞迴建立資料夾大小樹(treemap.build_size_tree)。"""

    progress = pyqtSignal(int, str)   # scanned_count, current_path
    finished = pyqtSignal(object)     # 根節點 dict(取消時為 None)

    def __init__(self, targets: list[str]):
        super().__init__()
        self._targets = targets
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        result = treemap.build_size_tree(
            self._targets,
            progress_cb=lambda count, path: self.progress.emit(count, path),
            cancel_check=lambda: self._cancelled,
        )
        self.finished.emit(result)


class SimilarityWorker(QObject):
    """相似圖片/影片偵測(感知雜湊)。opencv 只在 run() 內延遲載入,讓沒裝 opencv 的環境
    仍能啟動 App、其餘功能照常,只有這個掃描會回報錯誤。
    """

    progress = pyqtSignal(int, int, int, str)   # phase, done, total, current_path
    finished = pyqtSignal(list)                 # list[dict]:{"paths","segments","kind"}
    error = pyqtSignal(str)

    def __init__(self, targets: list[str], mode: str, threshold: int, min_match_seconds: int):
        super().__init__()
        self._targets = targets
        self._mode = mode                                 # "image" | "video"
        self._threshold = threshold                       # 圖片 Hamming 門檻
        self._min_match_seconds = min_match_seconds       # 影片最短相似片段(秒)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        try:
            from . import similarity
        except Exception as e:  # opencv / numpy 缺失或載入失敗
            self.error.emit(f"無法載入影像處理套件(opencv-python):{e}")
            self.finished.emit([])
            return

        emit = lambda phase, done, total, path: self.progress.emit(phase, done, total, path)
        cancel = lambda: self._cancelled
        if self._mode == "video":
            raw = similarity.find_similar_videos(
                self._targets, min_match_seconds=self._min_match_seconds, progress_cb=emit, cancel_check=cancel
            )
            result = [{"paths": d["paths"], "segments": d["segments"], "kind": "video"} for d in raw]
        else:
            raw = similarity.find_similar_images(
                self._targets, threshold=self._threshold, progress_cb=emit, cancel_check=cancel
            )
            result = [{"paths": g, "segments": [], "kind": "image"} for g in raw]
        self.finished.emit(result)


class DiagnosticWorker(QObject):
    """唯讀系統空間診斷:依序查詢 WinSxS / 驅動殘留 / 休眠檔 / 分頁檔 / System Restore。
    純讀取,不屬於 DEVDOC 原始規格,不提供任何刪除功能。
    """

    category_started = pyqtSignal(str)                 # category key
    category_finished = pyqtSignal(str, object)          # category key, result dict
    progress = pyqtSignal(str, int, int)                  # category key, count, bytes so far
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _cancel_check(self):
        return self._cancelled

    def run(self):
        for key, checker in (
            ("winsxs", diagnostics.winsxs_size),
            ("driverstore", diagnostics.driverstore_size),
        ):
            if self._cancelled:
                self.finished.emit()
                return
            self.category_started.emit(key)
            size, count, complete = checker(
                progress_cb=lambda c, b, k=key: self.progress.emit(k, c, b),
                cancel_check=self._cancel_check,
            )
            self.category_finished.emit(key, {"size": size, "count": count, "complete": complete})

        if self._cancelled:
            self.finished.emit()
            return

        self.category_started.emit("hiberfil")
        self.category_finished.emit("hiberfil", {"size": diagnostics.hibernation_file_size()})

        self.category_started.emit("pagefile")
        self.category_finished.emit("pagefile", {"size": diagnostics.pagefile_size()})

        self.category_started.emit("shadow_copy")
        self.category_finished.emit("shadow_copy", {"size": diagnostics.shadow_copy_used_size()})

        self.finished.emit()


class SmartHealthWorker(QObject):
    """磁碟健康(S.M.A.R.T.)診斷:依序查詢每顆實體磁碟,唯讀,不提供任何修復功能。"""

    device_found = pyqtSignal(dict)   # 每查完一顆磁碟就 emit 一次健康摘要 dict
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        for dev in smart_health.scan_devices():
            if self._cancelled:
                break
            device = dev.get("name")
            if not device:
                continue
            dev_type = dev.get("type")
            health = smart_health.query_health(device, dev_type) or {
                "device": device,
                "type": dev_type,
                "model": device,
                "passed": None,
            }
            self.device_found.emit(health)
        self.finished.emit()
