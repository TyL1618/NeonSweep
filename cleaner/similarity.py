"""相似圖片/影片偵測的純邏輯層(不碰 Qt)。與 analysis.find_duplicates 的「位元組完全相同」
精確比對刻意分離:這裡用感知雜湊(perceptual hash)做機率性相似判斷,會有誤判(刪除決定權
在使用者,UI 只呈現候選 + 縮圖供人工複核)。

- 圖片:dHash(相鄰像素亮度差)→ Hamming 距離分群。抓得到縮放/轉檔/重壓縮/亮度微調;
  抓不到裁切/局部塗改(那要 ORB/SIFT 特徵匹配,不在本模組範圍)。
- 影片:按「時間點」每 interval 秒取樣一幀算 dHash → 指紋序列;兩序列用 Smith-Waterman
  區域比對找最相似的連續片段,允許 gap 以處理掐頭去尾 / 抽掉中段的剪輯。
"""

import ctypes
import os
import time

import cv2
import numpy as np

from .analysis import EXCLUDED_DIRS, IMAGE_EXTS, PROGRESS_TIME_INTERVAL, VIDEO_EXTS
from .utils.fs import safe_walk

# 影片取樣參數
VIDEO_INTERVAL_SEC = 1.0     # 每幾秒取一幀
VIDEO_MAX_SAMPLES = 300      # 單片最多取樣幀數(上限,避免超長片把 DP 撐爆)
VIDEO_FRAME_THRESHOLD = 10   # 兩幀 dHash 視為「相同畫面」的 Hamming 上限

# Smith-Waterman 計分
_SW_MATCH = 2
_SW_MISMATCH = -1
_SW_GAP = -2


# ----------------------------------------------------------------------
# 指紋:dHash
# ----------------------------------------------------------------------


def _dhash_from_gray(gray) -> int:
    """輸入灰階影像,resize 到 9x8,相鄰像素亮度差 → 64-bit 指紋。"""
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]          # 8x8 布林
    packed = np.packbits(diff.flatten())          # 8 bytes
    return int.from_bytes(packed.tobytes(), "big")


def image_dhash(path: str) -> int | None:
    """讀圖算 dHash。用 imdecode(np.fromfile) 而非 cv2.imread,避開後者對 Windows 非 ASCII
    路徑讀取失敗的問題。讀不到 / 非影像回傳 None。
    """
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        return _dhash_from_gray(img)
    except Exception:
        return None


def _short_path(path: str) -> str | None:
    """取得 Windows 8.3 短路徑,供 cv2.VideoCapture 對付非 ASCII 路徑的後援。
    (8.3 產生被停用、或檔案不存在時回傳 None。)
    """
    try:
        GetShort = ctypes.windll.kernel32.GetShortPathNameW
        buf = ctypes.create_unicode_buffer(260)
        n = GetShort(path, buf, 260)
        if 0 < n < 260:
            return buf.value
    except Exception:
        pass
    return None


def _open_capture(path: str):
    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        return cap
    cap.release()
    short = _short_path(path)
    if short and short != path:
        cap = cv2.VideoCapture(short)
        if cap.isOpened():
            return cap
        cap.release()
    return None


def video_fingerprint(path: str, interval_sec: float = VIDEO_INTERVAL_SEC, max_samples: int = VIDEO_MAX_SAMPLES):
    """回傳 (指紋序列 list[int], 時長秒數)。讀不到回傳 (None, 0.0)。
    按時間點(CAP_PROP_POS_MSEC)取樣,故不受 FPS 差異影響。
    """
    cap = _open_capture(path)
    if cap is None:
        return None, 0.0
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        duration = (frame_count / fps) if fps > 0 else 0.0
        if duration <= 0:
            return None, 0.0
        hashes = []
        t = 0.0
        while t < duration and len(hashes) < max_samples:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hashes.append(_dhash_from_gray(gray))
            t += interval_sec
        return (hashes or None), duration
    except Exception:
        return None, 0.0
    finally:
        cap.release()


# ----------------------------------------------------------------------
# Hamming / popcount(向量化)
# ----------------------------------------------------------------------


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _popcount_u64(arr: np.ndarray) -> np.ndarray:
    """對任意 shape 的 uint64 陣列做逐元素 popcount,回傳同 shape 的計數陣列。"""
    flat = np.ascontiguousarray(arr, dtype="<u8")
    as_bytes = flat.view(np.uint8).reshape(-1, 8)
    counts = np.unpackbits(as_bytes, axis=1).sum(axis=1)
    return counts.reshape(arr.shape)


# ----------------------------------------------------------------------
# Union-Find
# ----------------------------------------------------------------------


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ----------------------------------------------------------------------
# 圖片相似分群
# ----------------------------------------------------------------------


def find_similar_images(targets, threshold=10, progress_cb=None, cancel_check=None):
    """回傳 list[list[str]],每組 >=2 張 Hamming 距離 <= threshold(遞移合併)的圖片。"""

    def cancelled():
        return bool(cancel_check and cancel_check())

    # 階段 1:蒐集 + 算指紋
    paths, hashes = [], []
    scanned = 0
    last_emit = 0.0
    for root in targets:
        for entry in safe_walk(root, exclude_dirs=EXCLUDED_DIRS):
            if cancelled():
                return []
            if os.path.splitext(entry.path)[1].lower() not in IMAGE_EXTS:
                continue
            h = image_dhash(entry.path)
            scanned += 1
            now = time.monotonic()
            if progress_cb and (scanned % 20 == 0 or (now - last_emit) >= PROGRESS_TIME_INTERVAL):
                last_emit = now
                progress_cb(1, scanned, 0, entry.path)
            if h is not None:
                paths.append(entry.path)
                hashes.append(h)

    n = len(hashes)
    if n < 2:
        return []

    # 階段 2:兩兩比對(向量化 popcount)→ Union-Find
    arr = np.array(hashes, dtype=np.uint64)
    uf = _UnionFind(n)
    last_emit = 0.0
    for i in range(n):
        if cancelled():
            return []
        now = time.monotonic()
        if progress_cb and (i % 10 == 0 or (now - last_emit) >= PROGRESS_TIME_INTERVAL):
            last_emit = now
            progress_cb(2, i, n, paths[i])
        if i + 1 >= n:
            break
        dists = _popcount_u64(arr[i + 1:] ^ arr[i])
        for off in np.nonzero(dists <= threshold)[0]:
            uf.union(i, i + 1 + int(off))

    groups: dict[int, list[str]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(paths[i])
    return [g for g in groups.values() if len(g) >= 2]


# ----------------------------------------------------------------------
# 影片相似:Smith-Waterman 區域比對
# ----------------------------------------------------------------------


def _match_matrix(a: np.ndarray, b: np.ndarray, frame_threshold: int) -> np.ndarray:
    """回傳 na×nb 布林矩陣:a[i] 與 b[j] 的 Hamming <= frame_threshold(視為同一畫面)。"""
    xor = a[:, None] ^ b[None, :]
    return _popcount_u64(xor) <= frame_threshold


def _local_align(match_rows, na: int, nb: int):
    """對布林 match 矩陣(list[list[bool]])跑 Smith-Waterman 區域比對。
    回傳 (best_score, a_start, a_end, b_start, b_end)(索引皆 0-based、含端點)。
    """
    prev = [0] * (nb + 1)
    # 只保留回溯所需的方向矩陣,H 用滾動列(省記憶體)
    H = [[0] * (nb + 1) for _ in range(na + 1)]
    best = 0
    bi = bj = 0
    for i in range(1, na + 1):
        mrow = match_rows[i - 1]
        Hi = H[i]
        Hp = H[i - 1]
        for j in range(1, nb + 1):
            s = _SW_MATCH if mrow[j - 1] else _SW_MISMATCH
            v = Hp[j - 1] + s
            u = Hp[j] + _SW_GAP
            l = Hi[j - 1] + _SW_GAP
            if u > v:
                v = u
            if l > v:
                v = l
            if v < 0:
                v = 0
            Hi[j] = v
            if v > best:
                best = v
                bi = i
                bj = j
    if best <= 0:
        return 0, 0, 0, 0, 0
    # 回溯到 0
    i, j = bi, bj
    while i > 0 and j > 0 and H[i][j] > 0:
        cur = H[i][j]
        s = _SW_MATCH if match_rows[i - 1][j - 1] else _SW_MISMATCH
        if cur == H[i - 1][j - 1] + s:
            i -= 1
            j -= 1
        elif cur == H[i - 1][j] + _SW_GAP:
            i -= 1
        else:
            j -= 1
    return best, i, bi - 1, j, bj - 1


def _fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def find_similar_videos(
    targets,
    min_match_len=20,
    frame_threshold=VIDEO_FRAME_THRESHOLD,
    interval_sec=VIDEO_INTERVAL_SEC,
    progress_cb=None,
    cancel_check=None,
):
    """回傳 list[dict],每組:{"paths": [...], "segments": [人類可讀的相似片段字串, ...]}。
    min_match_len:最短連續相似片段(取樣點數),過濾黑畫面/共用片頭之類的巧合匹配。
    """

    def cancelled():
        return bool(cancel_check and cancel_check())

    # 階段 1:蒐集 + 算指紋序列
    videos = []  # (path, np.uint64 array, duration)
    scanned = 0
    last_emit = 0.0
    for root in targets:
        for entry in safe_walk(root, exclude_dirs=EXCLUDED_DIRS):
            if cancelled():
                return []
            if os.path.splitext(entry.path)[1].lower() not in VIDEO_EXTS:
                continue
            scanned += 1
            if progress_cb:
                now = time.monotonic()
                if now - last_emit >= PROGRESS_TIME_INTERVAL:
                    last_emit = now
                    progress_cb(1, scanned, 0, entry.path)
            seq, duration = video_fingerprint(entry.path, interval_sec=interval_sec)
            if seq and len(seq) >= min_match_len:
                videos.append((entry.path, np.array(seq, dtype=np.uint64), duration))

    n = len(videos)
    if n < 2:
        return []

    # 階段 2:兩兩比對。先用向量化 match 矩陣做便宜的初篩(共享畫面數不足就跳過昂貴的 DP),
    # 只有可能重疊的配對才跑 Smith-Waterman。
    uf = _UnionFind(n)
    pair_detail: dict[tuple[int, int], str] = {}
    total_pairs = n * (n - 1) // 2
    done_pairs = 0
    last_emit = 0.0
    for i in range(n):
        if cancelled():
            return []
        pa, a, _da = videos[i]
        for j in range(i + 1, n):
            done_pairs += 1
            if progress_cb:
                now = time.monotonic()
                if now - last_emit >= PROGRESS_TIME_INTERVAL:
                    last_emit = now
                    progress_cb(2, done_pairs, total_pairs, pa)
            pb, b, _db = videos[j]
            match = _match_matrix(a, b, frame_threshold)
            # 初篩:a 有多少幀在 b 找得到近似畫面
            if int(match.any(axis=1).sum()) < min_match_len:
                continue
            best, a0, a1, b0, b1 = _local_align(match.tolist(), len(a), len(b))
            if best <= 0 or (a1 - a0 + 1) < min_match_len:
                continue
            uf.union(i, j)
            seg = (
                f"{os.path.basename(pa)} {_fmt_ts(a0 * interval_sec)}–{_fmt_ts(a1 * interval_sec)}"
                f"  ≈  {os.path.basename(pb)} {_fmt_ts(b0 * interval_sec)}–{_fmt_ts(b1 * interval_sec)}"
            )
            pair_detail[(i, j)] = seg

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        segments = [seg for (i, j), seg in pair_detail.items() if i in member_set and j in member_set]
        result.append({"paths": [videos[i][0] for i in members], "segments": segments})
    return result
