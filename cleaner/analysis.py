"""M4 分析功能的純邏輯層(大檔案分類/atime 偵測/重複檔三階段漏斗/開發空間偵測),
不碰任何 Qt 物件,方便獨立測試。Qt 層(workers.py 的 BigFileWorker/DupeWorker/DevSpaceWorker)
只負責把這裡的函式包進執行緒並轉發 progress/cancel。
"""

import datetime
import hashlib
import heapq
import os
import re
import time
import winreg

from .utils.fs import is_reparse_point, long_path, safe_walk

EXCLUDED_DIRS = [
    r"C:\Windows\WinSxS",
    r"C:\Windows\servicing",
    "System Volume Information",
    "$Recycle.Bin",
    os.path.expandvars(r"%LOCALAPPDATA%\NeonSweep"),
]

TOP_N_FILES = 200
PROGRESS_INTERVAL = 200
PROGRESS_TIME_INTERVAL = 0.1

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff"}
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".ogg"}
MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".onnx"}
IMAGE_DISK_EXTS = {".iso", ".img", ".vhd", ".vhdx", ".vmdk", ".wim"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz"}

DEVSPACE_DIR_NAMES = {"node_modules", ".venv", "venv", ".tox"}

_COPY_SUFFIX_RE = re.compile(r"[ _-]?\(\d+\)$")
_COPY_TRAILING = (" - 複製", " - copy", "_copy")


# ----------------------------------------------------------------------
# §8.1 用途分類器
# ----------------------------------------------------------------------

_PATH_RULES = [
    ("\\comfyui\\models\\checkpoints\\", "AI 模型", "Checkpoint 主模型"),
    ("\\comfyui\\models\\diffusion_models\\", "AI 模型", "擴散模型"),
    ("\\unet\\", "AI 模型", "擴散模型"),
    ("\\comfyui\\models\\loras\\", "AI 模型", "LoRA"),
    ("\\comfyui\\models\\controlnet\\", "AI 模型", "ControlNet"),
    ("\\comfyui\\models\\vae\\", "AI 模型", "VAE"),
    ("\\comfyui\\models\\clip\\", "AI 模型", "文字編碼器"),
    ("\\text_encoders\\", "AI 模型", "文字編碼器"),
    ("\\comfyui\\models\\upscale_models\\", "AI 模型", "放大模型"),
    ("\\comfyui\\models\\embeddings\\", "AI 模型", "Embedding"),
    ("\\steamapps\\", "遊戲", "Steam 遊戲檔"),
    ("\\epic games\\", "遊戲", "Epic 遊戲檔"),
]

_COMFYUI_MODELS_PREFIX = "\\comfyui\\models\\"
_FACEFUSION_FRAG = "\\facefusion\\"
_ASSETS_FRAG = "\\.assets\\"
_MODELS_FRAG = "\\models\\"


def classify(path: str) -> tuple[str, str]:
    """規則順序:先比對路徑規則,再比對副檔名,都沒中歸「其他」。全部不分大小寫。"""
    norm = path.replace("/", "\\").lower()

    for frag, category, role in _PATH_RULES:
        if frag in norm:
            return category, role

    idx = norm.find(_COMFYUI_MODELS_PREFIX)
    if idx != -1:
        rest = norm[idx + len(_COMFYUI_MODELS_PREFIX) :]
        subdir = rest.split("\\", 1)[0]
        if subdir:
            return "AI 模型", f"ComfyUI 模型({subdir})"

    if _FACEFUSION_FRAG in norm and (_ASSETS_FRAG in norm or _MODELS_FRAG in norm):
        return "AI 模型", "FaceFusion 模型"

    ext = os.path.splitext(norm)[1]
    if ext in MODEL_EXTS:
        return "AI 模型", "模型檔"
    if ext == ".bin" and "model" in norm:
        return "AI 模型", "模型檔"
    if ext in VIDEO_EXTS:
        return "影片", "影片檔"
    if ext in IMAGE_DISK_EXTS:
        return "映像檔", "映像檔"
    if ext in ARCHIVE_EXTS:
        return "壓縮檔", "壓縮檔"

    return "其他", ""


# 表格上方篩選 chip 的分組(「映像/壓縮」chip 同時涵蓋映像檔與壓縮檔兩個 category)
FILTER_GROUPS = {
    "all": None,
    "ai_model": {"AI 模型"},
    "video": {"影片"},
    "image_archive": {"映像檔", "壓縮檔"},
    "game": {"遊戲"},
    "other": {"其他"},
}
FILTER_LABELS = [
    ("all", "全部"),
    ("ai_model", "AI 模型"),
    ("video", "影片"),
    ("image_archive", "映像/壓縮"),
    ("game", "遊戲"),
    ("other", "其他"),
]


# ----------------------------------------------------------------------
# §8.1 atime 可靠性偵測
# ----------------------------------------------------------------------


def atime_reliable(drive_root: str) -> bool:
    """該磁碟的『最後存取時間』是否可信"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as k:
            val, _ = winreg.QueryValueEx(k, "NtfsDisableLastAccessUpdate")
    except OSError:
        return False
    mode = val & 0xF
    if mode in (1, 3):
        return False
    if mode == 0:
        return True
    sysdrive = os.environ.get("SystemDrive", "C:").upper()
    return drive_root.upper().startswith(sysdrive)


# ----------------------------------------------------------------------
# 共用:相對時間格式化
# ----------------------------------------------------------------------


def format_relative_time(ts: float) -> str:
    if not ts:
        return "—"
    dt = datetime.datetime.fromtimestamp(ts)
    days = max((datetime.datetime.now() - dt).days, 0)
    if days == 0:
        rel = "今天"
    elif days < 30:
        rel = f"{days} 天前"
    elif days < 365:
        rel = f"{days // 30} 個月前"
    else:
        rel = f"{days // 365} 年前"
    return f"{dt.strftime('%Y/%m/%d')}({rel})"


def is_stale(ts: float, threshold_days: int = 180) -> bool:
    if not ts:
        return False
    days = (datetime.datetime.now() - datetime.datetime.fromtimestamp(ts)).days
    return days > threshold_days


# ----------------------------------------------------------------------
# §8.1 大檔案 top-N 掃描(min-heap,記憶體恆定)
# ----------------------------------------------------------------------


class _Cancelled(Exception):
    """僅用於中止 scan_top_files 的內層迴圈,不對外拋出。"""


class _Throttle:
    def __init__(self, progress_cb, cancel_check):
        self._cb = progress_cb
        self._cancel_check = cancel_check
        self._count = 0
        self._last_time = 0.0

    def tick(self, path: str) -> int:
        if self._cancel_check and self._cancel_check():
            raise _Cancelled
        self._count += 1
        now = time.monotonic()
        if self._count % PROGRESS_INTERVAL == 0 or (now - self._last_time) >= PROGRESS_TIME_INTERVAL:
            self._last_time = now
            if self._cb:
                self._cb(self._count, path)
        return self._count


def scan_top_files(drives: list[str], progress_cb=None, cancel_check=None, top_n: int = TOP_N_FILES):
    """依序(非平行)掃描每顆磁碟,維護 top_n 最小堆。回傳依大小遞減排序的
    (size, path, mtime, atime) list。
    """
    heap: list[tuple[int, str, float, float]] = []
    throttle = _Throttle(progress_cb, cancel_check)

    for drive in drives:
        try:
            for entry in safe_walk(drive, exclude_dirs=EXCLUDED_DIRS):
                throttle.tick(entry.path)
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                item = (st.st_size, entry.path, st.st_mtime, st.st_atime)
                if len(heap) < top_n:
                    heapq.heappush(heap, item)
                elif item[0] > heap[0][0]:
                    heapq.heapreplace(heap, item)
        except _Cancelled:
            break

    return sorted(heap, key=lambda x: -x[0])


# ----------------------------------------------------------------------
# §8.3 重複檔案三階段漏斗
# ----------------------------------------------------------------------


def is_copy_style_name(filename: str) -> bool:
    stem = os.path.splitext(filename)[0]
    if _COPY_SUFFIX_RE.search(stem):
        return True
    lower = stem.lower()
    return any(lower.endswith(suf) for suf in _COPY_TRAILING)


def find_duplicates(
    drives: list[str],
    extensions: set[str] | None,
    min_size: int,
    progress_cb=None,
    cancel_check=None,
) -> list[list[str]]:
    """三階段漏斗:依大小分組 -> 前 4KB 快速雜湊 -> 全檔雜湊。回傳每組 >=2 個檔案的路徑清單。
    progress_cb(phase, done, total, current_path)。
    """

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    # 階段 1:依大小分組
    size_groups: dict[int, list[str]] = {}
    scanned = 0
    last_emit = 0.0
    for drive in drives:
        for entry in safe_walk(drive, exclude_dirs=EXCLUDED_DIRS):
            if cancelled():
                return []
            if extensions and os.path.splitext(entry.path)[1].lower() not in extensions:
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_size < min_size:
                continue
            scanned += 1
            now = time.monotonic()
            if scanned % PROGRESS_INTERVAL == 0 or (now - last_emit) >= PROGRESS_TIME_INTERVAL:
                last_emit = now
                if progress_cb:
                    progress_cb(1, scanned, 0, entry.path)
            size_groups.setdefault(st.st_size, []).append(entry.path)

    candidates = [paths for paths in size_groups.values() if len(paths) >= 2]

    # 階段 2:前 4KB 快速雜湊
    quick_groups: dict[tuple[int, bytes], list[str]] = {}
    total_candidates = sum(len(v) for v in candidates)
    done = 0
    last_emit = 0.0
    for paths in candidates:
        for path in paths:
            if cancelled():
                return []
            done += 1
            now = time.monotonic()
            if done % 50 == 0 or (now - last_emit) >= PROGRESS_TIME_INTERVAL:
                last_emit = now
                if progress_cb:
                    progress_cb(2, done, total_candidates, path)
            try:
                with open(long_path(path), "rb") as f:
                    chunk = f.read(4096)
                    size = os.fstat(f.fileno()).st_size
            except OSError:
                continue
            h = hashlib.blake2b(chunk).digest()
            quick_groups.setdefault((size, h), []).append(path)

    quick_candidates = [paths for paths in quick_groups.values() if len(paths) >= 2]

    # 階段 3:全檔雜湊
    full_groups: dict[str, list[str]] = {}
    total_full = sum(len(v) for v in quick_candidates)
    done = 0
    last_emit = 0.0
    for paths in quick_candidates:
        for path in paths:
            if cancelled():
                return []
            done += 1
            now = time.monotonic()
            if done % 20 == 0 or (now - last_emit) >= PROGRESS_TIME_INTERVAL:
                last_emit = now
                if progress_cb:
                    progress_cb(3, done, total_full, path)
            hasher = hashlib.blake2b()
            try:
                with open(long_path(path), "rb") as f:
                    while True:
                        block = f.read(1024 * 1024)
                        if not block:
                            break
                        hasher.update(block)
            except OSError:
                continue
            full_groups.setdefault(hasher.hexdigest(), []).append(path)

    return [paths for paths in full_groups.values() if len(paths) >= 2]


# ----------------------------------------------------------------------
# §8.4 開發空間掃描
# ----------------------------------------------------------------------


def _dir_size(path: str, cancel_check=None) -> int:
    total = 0
    for entry in safe_walk(path):
        if cancel_check and cancel_check():
            break
        try:
            total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _first_level_max_mtime(root: str, exclude_path: str) -> float:
    best = 0.0
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.path == exclude_path:
                    continue
                try:
                    best = max(best, entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return best


def find_devspaces(drives: list[str], progress_cb=None, cancel_check=None) -> list[dict]:
    """找目錄名屬於 DEVSPACE_DIR_NAMES(或旁有 Cargo.toml 的 target)。
    找到即停止深入該目錄,但另外計算其總大小。
    """
    results: list[dict] = []

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    for drive in drives:
        stack = [drive]
        while stack:
            if cancelled():
                return results
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except (PermissionError, OSError):
                continue

            for entry in entries:
                if cancelled():
                    return results
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if is_reparse_point(entry):
                        continue
                except OSError:
                    continue

                norm_path = os.path.normcase(entry.path)
                if any(os.path.normcase(ex) in norm_path for ex in EXCLUDED_DIRS):
                    continue

                name = entry.name
                is_target = name == "target" and os.path.isfile(
                    os.path.join(os.path.dirname(entry.path), "Cargo.toml")
                )

                if name.lower() in DEVSPACE_DIR_NAMES or is_target:
                    size = _dir_size(entry.path, cancel_check)
                    project_root = os.path.dirname(entry.path)
                    last_activity = _first_level_max_mtime(project_root, entry.path)
                    results.append(
                        {
                            "project_path": project_root,
                            "cache_path": entry.path,
                            "kind": name,
                            "size": size,
                            "last_activity": last_activity,
                        }
                    )
                    if progress_cb:
                        progress_cb(entry.path)
                    # 找到即停止深入,不進入該目錄繼續遞迴
                else:
                    stack.append(entry.path)

    return results
