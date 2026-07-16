"""相似圖片/影片偵測的純邏輯層(不碰 Qt)。與 analysis.find_duplicates 的「位元組完全相同」
精確比對刻意分離:這裡用感知雜湊(perceptual hash)做機率性相似判斷,會有誤判(刪除決定權
在使用者,UI 只呈現候選 + 縮圖供人工複核)。

- 圖片:dHash(相鄰像素亮度差)→ Hamming 距離分群。抓得到縮放/轉檔/重壓縮/亮度微調;
  抓不到裁切/局部塗改(那要 ORB/SIFT 特徵匹配,不在本模組範圍)。
- 影片:兩階段「粗篩 → 精修」。每部影片先用**依全片長度自動放寬的間隔**取樣(保證固定
  樣本數就能涵蓋全片,不會漏掉任何一段——短片仍維持 1 秒精細度不受影響),粗篩找出候選
  重疊區間後,只針對那個小範圍時間窗重新用 1 秒間隔取樣、重新比對,拿到精確到秒的邊界。
  兩階段都用 Smith-Waterman 區域比對,允許 gap 以處理掐頭去尾 / 抽掉中段的剪輯。
"""

import ctypes
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2
import numpy as np

from .analysis import EXCLUDED_DIRS, IMAGE_EXTS, PROGRESS_TIME_INTERVAL, VIDEO_EXTS
from .utils.fs import safe_walk

# 只壓 OpenCV 自己的 [ERROR:...] log(解不開的圖片/影片本來就會被 image_dhash/build_video_print
# 的 try/except 正常跳過,不影響掃描結果)。libpng 的 "libpng warning: ..." 是另一個函式庫自己
# 寫死輸出到 stderr 的行為,這裡管不到、壓不掉。打包後的 exe 是 console=False,兩種輸出使用者
# 都看不到,這裡只是降低開發時終端機的雜訊。
cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)

# 影片取樣參數
VIDEO_INTERVAL_SEC = 1.0     # 每幾秒取一幀
VIDEO_MAX_SAMPLES = 300      # 單片最多取樣幀數(上限,避免超長片把 DP 撐爆)
VIDEO_FRAME_THRESHOLD = 10   # 兩幀 dHash 視為「相同畫面」的 Hamming 上限
VIDEO_FINGERPRINT_WORKERS = 4  # 平行算指紋的執行緒數,偏保守——傳統硬碟上開太多平行 seek 反而互相拖累

# 浮水印寬容度:算指紋前先裁掉四邊各這個比例,避開角落/邊緣常見的浮水印位置。
# 只用在「影片」取樣路徑(_sample_window/build_video_print),刻意不動 image_dhash(圖片路徑)。
WATERMARK_CROP_MARGIN = 0.10

# Smith-Waterman 計分
_SW_MATCH = 2
_SW_MISMATCH = -1
_SW_GAP = -2


# ----------------------------------------------------------------------
# 指紋:dHash
# ----------------------------------------------------------------------


def _dhash_from_gray(gray, crop_margin: float = 0.0) -> int:
    """輸入灰階影像,resize 到 9x8,相鄰像素亮度差 → 64-bit 指紋。

    crop_margin > 0 時,resize 前先把四邊各裁掉這個比例(浮水印寬容度用,見
    WATERMARK_CROP_MARGIN)。預設 0 完全不裁,行為與改動前一致——只有影片取樣路徑會傳非零值,
    image_dhash(圖片路徑)不受影響。
    """
    if crop_margin > 0:
        h, w = gray.shape[:2]
        mx, my = int(w * crop_margin), int(h * crop_margin)
        if mx > 0 and my > 0 and w - 2 * mx > 0 and h - 2 * my > 0:
            gray = gray[my : h - my, mx : w - mx]
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


def _sample_window(path: str, start_sec: float, end_sec: float, interval_sec: float, max_samples: int | None = None):
    """對 [start_sec, end_sec) 這段時間窗,按 interval_sec 取樣算 dHash,回傳 list[int]。
    供粗篩(全片,start=0/end=duration)與精修(候選片段附近的小窗)共用。
    """
    cap = _open_capture(path)
    if cap is None:
        return []
    try:
        hashes = []
        t = max(0.0, start_sec)
        while t < end_sec and (max_samples is None or len(hashes) < max_samples):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hashes.append(_dhash_from_gray(gray, crop_margin=WATERMARK_CROP_MARGIN))
            t += interval_sec
        return hashes
    except Exception:
        return []
    finally:
        cap.release()


def build_video_print(path: str, base_interval: float = VIDEO_INTERVAL_SEC, max_samples: int = VIDEO_MAX_SAMPLES):
    """單一影片的「原生」粗篩指紋。間隔依全片長度自動放寬(`max(base_interval, duration/max_samples)`),
    保證固定的 max_samples 個樣本點就能涵蓋全片——不會像固定間隔那樣,長片只取樣得到前面一小段
    (例如 60 分鐘的影片若固定每秒取樣、上限 300 個樣本,只能涵蓋前 5 分鐘,中後段被剪出來的
    片段會完全偵測不到)。**時長 <= base_interval × max_samples 的短片,間隔仍是 base_interval,
    精細度完全不受影響**——放寬只發生在真正需要的長片上。

    回傳 dict {"path","duration","interval","hashes"(np.uint64 array)},讀不到回傳 None。
    """
    cap = _open_capture(path)
    if cap is None:
        return None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        duration = (frame_count / fps) if fps > 0 else 0.0
        if duration <= 0:
            return None
        interval = max(base_interval, duration / max_samples)
        hashes = []
        t = 0.0
        while t < duration and len(hashes) < max_samples:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hashes.append(_dhash_from_gray(gray, crop_margin=WATERMARK_CROP_MARGIN))
            t += interval
        if not hashes:
            return None
        return {"path": path, "duration": duration, "interval": interval, "hashes": np.array(hashes, dtype=np.uint64)}
    except Exception:
        return None
    finally:
        cap.release()


def _estimate_offset(va: dict, vb: dict, frame_threshold: int):
    """兩部影片的粗篩指紋(間隔、相位都可能不同,不要求對齊)兩兩比對,每一對匹配的樣本都能
    估出一個「B 在 A 時間軸上的起點位移」;把這些位移粗略分桶投票,回傳票數最多的位移候選。

    這裡刻意不用「把兩邊指紋硬併到同一個間隔/相位再跑 Smith-Waterman」的做法——短片自己的
    取樣起點跟長片的粗篩取樣起點通常對不上相位(兩者都是各自從 t=0 開始,而真正重疊的位移
    量是未知數,不會剛好是間隔的整數倍),硬併只會讓兩邊的取樣點集合幾乎不重疊、比對失敗。
    改成不管相位、直接抓「哪一對樣本內容相近」,再用位移投票找出最可能的重疊位置,不受兩邊
    間隔/相位不同影響。回傳 (offset_seconds, votes) 或 (None, 0)。
    """
    match = _match_matrix(va["hashes"], vb["hashes"], frame_threshold)
    if not match.any():
        return None, 0
    ia, ib = np.nonzero(match)
    offsets = ia.astype(np.float64) * va["interval"] - ib.astype(np.float64) * vb["interval"]
    bucket = max(va["interval"], vb["interval"])
    buckets = np.round(offsets / bucket).astype(np.int64)
    values, counts = np.unique(buckets, return_counts=True)
    best_idx = int(np.argmax(counts))
    return float(values[best_idx]) * bucket, int(counts[best_idx])


# ----------------------------------------------------------------------
# Hamming / popcount(向量化)
# ----------------------------------------------------------------------


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


if hasattr(np, "bitwise_count"):

    def _popcount_u64(arr: np.ndarray) -> np.ndarray:
        """對任意 shape 的 uint64 陣列做逐元素 popcount,回傳同 shape 的計數陣列。
        用 np.bitwise_count(numpy>=2.0)直接向量化算,實測比舊版的
        unpackbits(把每個 byte 展開成 8 個獨立 uint8 再加總)快約 240 倍——這個函式是
        影片兩兩比對(_estimate_offset/_match_matrix)最熱的路徑,大量影片時差距很有感。
        """
        return np.bitwise_count(np.ascontiguousarray(arr, dtype="<u8"))

else:

    def _popcount_u64(arr: np.ndarray) -> np.ndarray:
        """numpy < 2.0 沒有 bitwise_count 的後援實作,較慢但正確性一致。"""
        flat = np.ascontiguousarray(arr, dtype="<u8")
        as_bytes = flat.view(np.uint8).reshape(-1, 8)
        counts = np.unpackbits(as_bytes, axis=1).sum(axis=1)
        return counts.reshape(arr.shape)


# ----------------------------------------------------------------------
# 圖片相似分群
# ----------------------------------------------------------------------


def find_similar_images(targets, threshold=10, progress_cb=None, cancel_check=None):
    """回傳 list[list[str]],每組內任兩張圖的 Hamming 距離保證 <= threshold*2(見
    _anchor_clusters:錨點分群,不用 Union-Find 遞移合併,避免「A~B~C,但 A、C 完全不像」
    的長鏈問題——這在 Windows 佈景主題這類平滑漸層桌布上曾經把上萬張互不相似的圖片
    焊成一組,已修掉)。
    """

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

    # 階段 2:錨點分群(向量化 popcount)。
    arr = np.array(hashes, dtype=np.uint64)
    assigned = np.zeros(n, dtype=bool)
    groups: list[list[int]] = []
    last_emit = 0.0
    for i in range(n):
        if cancelled():
            return []
        now = time.monotonic()
        if progress_cb and (i % 10 == 0 or (now - last_emit) >= PROGRESS_TIME_INTERVAL):
            last_emit = now
            progress_cb(2, i, n, paths[i])
        if assigned[i]:
            continue
        assigned[i] = True
        if i + 1 >= n:
            continue
        later_mask = np.zeros(n, dtype=bool)
        later_mask[i + 1 :] = ~assigned[i + 1 :]
        later_idx = np.nonzero(later_mask)[0]
        if later_idx.size == 0:
            continue
        dists = _popcount_u64(arr[later_idx] ^ arr[i])
        matched = later_idx[dists <= threshold]
        if matched.size:
            assigned[matched] = True
            groups.append([i] + matched.tolist())

    return [[paths[idx] for idx in g] for g in groups if len(g) >= 2]


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


def _refine_match(
    path_a: str,
    path_b: str,
    ta0: float,
    ta1: float,
    tb0: float,
    tb1: float,
    frame_threshold: int,
    base_interval: float = VIDEO_INTERVAL_SEC,
):
    """精修階段:粗篩只給了「大概哪一段對得上」的候選時間窗(可能因為粗篩間隔較粗而不準)。
    這裡只針對候選窗附近(留一點 margin 免得邊界剛好切到)重新用 base_interval 密集取樣,
    重新跑一次(範圍很小、成本低的)Smith-Waterman,拿到精確到秒的邊界,同時二次驗證排除
    粗篩階段的巧合匹配。回傳 (a_start,a_end,b_start,b_end,matched_seconds) 或 None(驗證失敗)。
    """
    margin = base_interval * 4
    a_start, a_end = max(0.0, ta0 - margin), ta1 + margin
    b_start, b_end = max(0.0, tb0 - margin), tb1 + margin

    hash_a = _sample_window(path_a, a_start, a_end, base_interval)
    hash_b = _sample_window(path_b, b_start, b_end, base_interval)
    if len(hash_a) < 2 or len(hash_b) < 2:
        return None

    match = _match_matrix(np.array(hash_a, dtype=np.uint64), np.array(hash_b, dtype=np.uint64), frame_threshold)
    best, a0, a1, b0, b1 = _local_align(match.tolist(), len(hash_a), len(hash_b))
    if best <= 0:
        return None
    matched_sec = min(a1 - a0 + 1, b1 - b0 + 1) * base_interval
    return (
        a_start + a0 * base_interval,
        a_start + (a1 + 1) * base_interval,
        b_start + b0 * base_interval,
        b_start + (b1 + 1) * base_interval,
        matched_sec,
    )


def find_similar_videos(
    targets,
    min_match_seconds=20,
    frame_threshold=VIDEO_FRAME_THRESHOLD,
    base_interval=VIDEO_INTERVAL_SEC,
    max_samples=VIDEO_MAX_SAMPLES,
    progress_cb=None,
    cancel_check=None,
    fingerprint_workers=VIDEO_FINGERPRINT_WORKERS,
):
    """回傳 list[dict],每組:{"paths": [...], "segments": [人類可讀的相似片段字串, ...]}。
    min_match_seconds:最短連續相似片段(秒),過濾黑畫面/共用片頭之類的巧合匹配。
    fingerprint_workers:階段 1 平行算指紋的執行緒數(cv2 解碼會釋放 GIL,執行緒能真的平行跑)。

    兩階段「粗篩→精修」(見模組開頭說明):
    - 粗篩:每部影片各自用 build_video_print 算出「涵蓋全片」的原生指紋(短片維持
      base_interval 精細度,長片自動放寬間隔)。**兩邊都還沒被放寬過**(短片對短片)時,兩者
      本來就是同一個間隔,直接用 Smith-Waterman 對齊即可。只要**任一邊被放寬過**(牽涉到長
      片),就改用 `_estimate_offset` 的位移投票:不要求兩邊取樣點對齊到同一個網格相位
      (兩邊各自從 t=0 開始取樣,真正的重疊位移是未知數,不會剛好是取樣間隔的整數倍,若硬
      要對齊網格反而會讓兩邊的取樣點集合幾乎不重疊、比對失敗),而是直接找出「哪些樣本對內容
      相近」,再用這些配對估出的位移做投票,取票數最高的位移當候選重疊窗。
    - 精修:候選窗只是「大概哪一段」,接下來只針對那個小時間窗附近(含 margin)重新用
      base_interval 密集取樣、重新比對,拿到精確到秒的邊界並二次驗證排除巧合,真正的
      min_match_seconds 門檻在這裡把關。
    """

    def cancelled():
        return bool(cancel_check and cancel_check())

    # 階段 1a:先蒐集所有影片路徑(快,不必平行化),這樣才能事先知道總數給進度條用。
    paths = []
    for root in targets:
        for entry in safe_walk(root, exclude_dirs=EXCLUDED_DIRS):
            if cancelled():
                return []
            if os.path.splitext(entry.path)[1].lower() in VIDEO_EXTS:
                paths.append(entry.path)

    # 階段 1b:平行算每部影片的粗篩指紋(cv2 的解碼呼叫會釋放 GIL,實測確實有平行加速)。
    # worker 數偏保守——傳統硬碟上開太多平行 seek 反而會互相拖累。
    videos = []  # list[dict]:build_video_print 的回傳(順序不保證跟 paths 一致,分群不受影響)
    total = len(paths)
    scanned = 0
    last_emit = 0.0
    with ThreadPoolExecutor(max_workers=fingerprint_workers) as executor:
        futures = {
            executor.submit(build_video_print, p, base_interval, max_samples): p for p in paths
        }
        for fut in as_completed(futures):
            if cancelled():
                executor.shutdown(wait=False, cancel_futures=True)
                return []
            scanned += 1
            if progress_cb:
                now = time.monotonic()
                if now - last_emit >= PROGRESS_TIME_INTERVAL:
                    last_emit = now
                    progress_cb(1, scanned, total, futures[fut])
            vp = fut.result()
            if vp is not None and vp["duration"] >= min_match_seconds:
                videos.append(vp)

    n = len(videos)
    if n < 2:
        return []

    # 階段 2:錨點分群(不用 Union-Find)。每部影片只跟「錨點」比對,匹配上就收進錨點的組、
    # 標記為已分組,之後不會再被其他錨點搶走、也不會再去測試別人。這避免了 Union-Find 遞移
    # 合併的長鏈問題(A 跟 B 像、B 跟 C 像,但 A、C 完全不像,三個卻被焊進同一組)——圖片那邊
    # 已經證實過會發生(見 find_similar_images 開頭說明),影片理論上一樣有風險,一併修掉。
    # 附帶好處:已分組的影片會被整個跳過,不會再被拿去跟後面的錨點比對,省下不少昂貴的
    # 位移投票/精修運算。
    assigned = [False] * n
    total_pairs_estimate = n * (n - 1) // 2  # 只用來給進度條一個大致分母,不追求精確
    done_pairs = 0
    last_emit = 0.0
    result = []
    for i in range(n):
        if cancelled():
            return []
        if assigned[i]:
            continue
        assigned[i] = True
        va = videos[i]
        members = [i]
        segments = []
        for j in range(i + 1, n):
            if assigned[j]:
                continue
            done_pairs += 1
            if progress_cb:
                now = time.monotonic()
                if now - last_emit >= PROGRESS_TIME_INTERVAL:
                    last_emit = now
                    progress_cb(2, done_pairs, total_pairs_estimate, va["path"])
            vb = videos[j]
            both_fine = va["interval"] <= base_interval * 1.0001 and vb["interval"] <= base_interval * 1.0001

            if both_fine:
                # 兩邊都還沒被放寬(短片對短片),原生指紋本來就是同一個間隔,兩邊取樣起點都是
                # 各自的 t=0、相位天生一致,直接對齊即可,不必再解一次影片。
                match = _match_matrix(va["hashes"], vb["hashes"], frame_threshold)
                if not match.any():
                    continue
                best, a0, a1, b0, b1 = _local_align(match.tolist(), len(va["hashes"]), len(vb["hashes"]))
                if best <= 0:
                    continue
                matched_sec = min(a1 - a0 + 1, b1 - b0 + 1) * base_interval
                if matched_sec < min_match_seconds:
                    continue
                fa0, fa1 = a0 * base_interval, (a1 + 1) * base_interval
                fb0, fb1 = b0 * base_interval, (b1 + 1) * base_interval
            else:
                # 至少一邊牽涉到長片(間隔被放寬過):兩邊取樣的相位不保證對齊,改用位移投票
                # 找出候選重疊窗,再把 B 的全長投影到 A 的時間軸上,只在那個窗附近精修。
                offset, votes = _estimate_offset(va, vb, frame_threshold)
                if offset is None or votes < 2:
                    continue
                ta0 = max(0.0, offset)
                ta1 = min(va["duration"], offset + vb["duration"])
                if ta1 - ta0 < min_match_seconds:
                    continue
                tb0, tb1 = ta0 - offset, ta1 - offset
                refined = _refine_match(va["path"], vb["path"], ta0, ta1, tb0, tb1, frame_threshold, base_interval)
                if refined is None:
                    continue
                fa0, fa1, fb0, fb1, matched_sec = refined
                if matched_sec < min_match_seconds:
                    continue

            assigned[j] = True
            members.append(j)
            segments.append(
                f"{os.path.basename(va['path'])} {_fmt_ts(fa0)}–{_fmt_ts(fa1)}"
                f"  ≈  {os.path.basename(vb['path'])} {_fmt_ts(fb0)}–{_fmt_ts(fb1)}"
            )

        if len(members) >= 2:
            result.append({"paths": [videos[idx]["path"] for idx in members], "segments": segments})

    return result
