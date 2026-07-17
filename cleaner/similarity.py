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
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2
import numpy as np

from . import print_cache
from .analysis import EXCLUDED_DIRS, IMAGE_EXTS, PROGRESS_TIME_INTERVAL, VIDEO_EXTS
from .utils.fs import safe_walk

# 效能儀表用(見 find_similar_videos 結尾的 logger.info)。這裡只取 logger、不掛 handler
# 也不設 level——那是應用層的事(main.py 設檔案 handler,因為打包後 console=False,
# 印到 stderr 使用者根本看不到)。純邏輯層自己設定 handler 會蓋掉呼叫端的配置。
logger = logging.getLogger(__name__)

# 只壓 OpenCV 自己的 [ERROR:...] log(解不開的圖片/影片本來就會被 image_dhash/build_video_print
# 的 try/except 正常跳過,不影響掃描結果)。libpng 的 "libpng warning: ..." 是另一個函式庫自己
# 寫死輸出到 stderr 的行為,這裡管不到、壓不掉。打包後的 exe 是 console=False,兩種輸出使用者
# 都看不到,這裡只是降低開發時終端機的雜訊。
cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)

# OpenCV 自己的 parallel_for 執行緒池:這個模組餵給 cv2 的影像都極小(resize 到 9x8、
# cvtColor 一張畫面),平行化拿不到任何好處,只會多開一堆執行緒跟 ffmpeg 的解碼執行緒搶核心。
# 關掉可以少一個 CPU 壓力來源(真正的大頭是 ffmpeg 解碼,那個要靠降低程序優先權處理,
# 見 utils/proc.py)。
cv2.setNumThreads(1)

# 影片取樣參數
VIDEO_INTERVAL_SEC = 1.0     # 每幾秒取一幀
VIDEO_MAX_SAMPLES = 300      # 單片最多取樣幀數(上限,避免超長片把 DP 撐爆)
VIDEO_REFINE_MAX_SAMPLES = 600  # 精修階段單邊取樣上限:候選窗可能很長(兩部都是長片、offset≈0
                                # 時窗長≈整片),必須跟粗篩一樣有上限,否則解碼次數與 Smith-Waterman
                                # 的 O(na*nb) DP 會隨窗長無上限膨脹(見 _refine_match)
VIDEO_FRAME_THRESHOLD = 10   # 兩幀 dHash 視為「相同畫面」的 Hamming 上限

# 階段 2 每隔幾秒寫一行進度到 log。大型影片庫的階段 2 要跑數小時,只在結束時記錄的話,
# 掃到一半去看 log、或中途按取消,都完全拿不到數字——正要拿它診斷效能時最需要的就是那些數字。
PHASE2_LOG_INTERVAL = 60.0

# 平行算指紋的執行緒數,偏保守——傳統硬碟上開太多平行 seek 反而互相拖累。
# 4 是沒有實測數據下的猜測(見 DEVDOC §8.6);HDD 的最佳值跟磁碟、影片大小、檔案碎片化都有關,
# 只能在真實影片庫上量。用環境變數覆寫,方便在目標機器上 A/B 找出甜蜜點,不必改程式重打包:
#     set NEONSWEEP_FP_WORKERS=2 && NeonSweep.exe
# 每次掃描的耗時會寫進 log(見 find_similar_videos),直接比對就知道哪個值好。
VIDEO_FINGERPRINT_WORKERS = 4
try:
    _env_workers = int(os.environ.get("NEONSWEEP_FP_WORKERS", ""))
    if 1 <= _env_workers <= 32:
        VIDEO_FINGERPRINT_WORKERS = _env_workers
except ValueError:
    pass

# 圖片退化指紋門檻:純色圖 dHash 全為 0、平滑漸層圖可能全為 1,這類圖的 dHash 幾乎沒有鑑別力
# (純黑圖彼此距離恆為 0),會製造大量假群組。popcount(1 的個數)落在 [DEGENERATE, 64-DEGENERATE]
# 之外的視為退化指紋,不納入分群。門檻取得很保守,只濾掉近乎純色 / 近乎完美漸層。
IMAGE_DEGENERATE_POPCOUNT = 3

# 影片退化幀門檻:黑畫面轉場、片頭卡司名單底色、純色淡入淡出這類低鑑別力畫面,在不同影片間
# 很容易互相 Hamming 命中,會讓 find_similar_videos 對大量互不相關的影片誤觸發昂貴的
# Smith-Waterman DP / 精修重解碼。與圖片路徑同一套 popcount 判準,但保留成獨立常數,因為
# 影片幀跟靜態圖片的統計特性不保證一致,未來可能需要各自調整。退化幀不會被移出取樣陣列
# (陣列長度/間隔仍用來換算時間軸),只在比對時強制視為不匹配(見 _match_matrix 的 deg_a/deg_b)。
VIDEO_DEGENERATE_POPCOUNT = 3

# 浮水印寬容度:算指紋前先裁掉四邊各這個比例,避開角落/邊緣常見的浮水印位置。
# 只用在「影片」取樣路徑(_sample_window/build_video_print),刻意不動 image_dhash(圖片路徑)。
WATERMARK_CROP_MARGIN = 0.10

# 短片提前跳過解碼的安全底線。**必須 <= views/similarity_page.py 的 VIDEO_STRICTNESS_OPTIONS
# 裡最寬鬆檔位的 min_match_seconds(目前「非常寬鬆」= 4 秒)**——刻意不直接用呼叫端傳進來的
# min_match_seconds(那是 UI 可調的),因為指紋要進快取,而快取的存在意義就是跨掃描重用
# (見 find_similar_videos 的 cache 說明:「存快取要在 min_match_seconds 門檻之前」)。
# 如果拿目前這次的門檻當跳過標準,使用者從「標準」20 秒改選「非常寬鬆」4 秒後,4~20 秒
# 之間的影片會發現快取沒有它們、要重新解碼——等於重新引入同一份文件警告過的那個 bug。
# 這裡改用一個遠低於任何 UI 選項的固定常數,只濾掉真正不可能配對成功的極短片(使用者
# 硬碟上大量 3 秒級的片段),不隨這次掃描的門檻變動,快取語意保持跨掃描一致。
VIDEO_MIN_CACHEABLE_DURATION = 2.0

# 取樣後端(指紋 dict 的 "backend" 欄位)。uniform = 等距時間點取樣(cv2,每個點都要
# 從前一個關鍵幀解碼到目標時間);keyframe = 只解關鍵幀(PyAV,快得多但取樣點不等距)。
# 兩者的指紋 dict 形狀相同,差別只在 times 是否等距——比對端一律吃 times,不必分兩套邏輯。
BACKEND_UNIFORM = "uniform"

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

    效能:先用 IMREAD_REDUCED_GRAYSCALE_8 以 1/8 尺寸解碼(JPEG 直接在 DCT 域縮小,大圖可快
    數倍),反正終點是 9x8 dHash,先縮到 1/8 再 resize 精度幾乎無損。縮完任一邊 < 9 時(小圖、
    或不支援縮小解碼的格式回傳過小影像)退回全解析度重解一次。注意:縮小解碼與全解析度解碼的
    dHash 不保證逐 bit 相同(INTER_AREA 平均的來源像素不同),但差異僅一兩個 bit,遠在相似門檻
    (6~14 bit)之內——這是速度與精度的取捨,不像 popcount 那次是完全等價的無損替換。
    """
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        img = cv2.imdecode(data, cv2.IMREAD_REDUCED_GRAYSCALE_8)
        if img is None or img.shape[0] < 9 or img.shape[1] < 9:
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


def _sample_window(
    path: str,
    start_sec: float,
    end_sec: float,
    interval_sec: float,
    max_samples: int | None = None,
    cancel_check=None,
):
    """對 [start_sec, end_sec) 這段時間窗,按 interval_sec 取樣算 dHash,回傳 list[int]。
    供粗篩(全片,start=0/end=duration)與精修(候選片段附近的小窗)共用。
    cancel_check:精修可能對長片密集取樣、耗時較久,每個樣本都檢查一次取消旗標。
    """
    cap = _open_capture(path)
    if cap is None:
        return []
    try:
        hashes = []
        t = max(0.0, start_sec)
        while t < end_sec and (max_samples is None or len(hashes) < max_samples):
            if cancel_check and cancel_check():
                break
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


def build_video_print(
    path: str,
    base_interval: float = VIDEO_INTERVAL_SEC,
    max_samples: int = VIDEO_MAX_SAMPLES,
    cancel_check=None,
    min_duration: float = 0.0,
):
    """單一影片的「原生」粗篩指紋。間隔依全片長度自動放寬(`max(base_interval, duration/max_samples)`),
    保證固定的 max_samples 個樣本點就能涵蓋全片——不會像固定間隔那樣,長片只取樣得到前面一小段
    (例如 60 分鐘的影片若固定每秒取樣、上限 300 個樣本,只能涵蓋前 5 分鐘,中後段被剪出來的
    片段會完全偵測不到)。**時長 <= base_interval × max_samples 的短片,間隔仍是 base_interval,
    精細度完全不受影響**——放寬只發生在真正需要的長片上。

    回傳 dict {"path","duration","interval","hashes"(np.uint64),"times"(np.float64,
    每個樣本的實際時間戳),"backend"},讀不到回傳 None。

    `times` 在這條均勻取樣路徑就是 `arange(n) * interval`,看似多餘,但指紋 dict 的形狀要跟
    非均勻取樣的後端(見 "backend" 欄位)一致,比對端才能一律用真實時間戳算位移、不必分兩套
    邏輯;快取 schema 也因此不用隨後端增加而遷移。

    cancel_check:**這是取消按鈕能不能即時生效的關鍵**(DEVDOC §10.1:長迴圈的取消要放在真正
    耗時的內層)。每個樣本(一次 seek + 解碼)都檢查一次——這個函式是階段 1 平行執行緒裡真正
    在跑的工作,取消旗標設下去之後,已經在解的影片如果不檢查這個,會解完全部 max_samples
    (最多 300 次 seek)才會停,使用者按下取消要等好一陣子才有反應。取消時回傳 None(等同
    「這部沒解出東西」),跟其他失敗路徑一致——反正整個掃描都要中止了,這部有沒有指紋不重要。
    """
    cap = _open_capture(path)
    if cap is None:
        return None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        duration = (frame_count / fps) if fps > 0 else 0.0
        if duration <= 0 or duration < min_duration:
            # 只在確定拿到時長之後才提前回傳(這一步是免費的 metadata 讀取,不是 seek);
            # 真正省下的是接下來的取樣迴圈(最多 max_samples 次 seek+decode)。
            return None
        interval = max(base_interval, duration / max_samples)
        hashes = []
        t = 0.0
        while t < duration and len(hashes) < max_samples:
            if cancel_check and cancel_check():
                return None
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hashes.append(_dhash_from_gray(gray, crop_margin=WATERMARK_CROP_MARGIN))
            t += interval
        if not hashes:
            return None
        hashes_arr = np.array(hashes, dtype=np.uint64)
        return {
            "path": path,
            "duration": duration,
            "interval": interval,
            "hashes": hashes_arr,
            "times": np.arange(len(hashes), dtype=np.float64) * interval,
            "backend": BACKEND_UNIFORM,
            "degenerate": _degenerate_mask(hashes_arr),
        }
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
    間隔/相位不同影響。

    回傳 (offset_seconds, votes, hit_span) 或 (None, 0, None)。`hit_span` 是「投給勝出位移的
    那些樣本」在 A 時間軸上的 (最早, 最晚) 時間——也就是**證據實際落在哪個範圍**。精修只需要
    看這個範圍,不必掃整個幾何重疊(見 find_similar_videos 呼叫處的說明,這是階段 2 最大的
    成本來源)。
    """
    match = _match_matrix(va["hashes"], vb["hashes"], frame_threshold, va["degenerate"], vb["degenerate"])
    if not match.any():
        return None, 0, None
    ia, ib = np.nonzero(match)
    # 用真實時間戳而不是「索引 × 間隔」:等距取樣時兩者相同,但非等距取樣的後端(見
    # BACKEND_UNIFORM 說明)只有 times 是對的,這樣位移計算不必分兩套邏輯。
    ta = va["times"][ia]
    offsets = ta - vb["times"][ib]
    bucket = max(va["interval"], vb["interval"])
    buckets = np.round(offsets / bucket).astype(np.int64)
    values, counts = np.unique(buckets, return_counts=True)
    best_idx = int(np.argmax(counts))
    hits = ta[buckets == values[best_idx]]
    return float(values[best_idx]) * bucket, int(counts[best_idx]), (float(hits.min()), float(hits.max()))


# ----------------------------------------------------------------------
# Hamming / popcount(向量化)
# ----------------------------------------------------------------------


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


def _degenerate_mask(hashes: np.ndarray) -> np.ndarray:
    """對一組 dHash 逐一判斷是否為退化指紋(見 VIDEO_DEGENERATE_POPCOUNT),回傳同 shape 的
    布林陣列。退化幀在比對時會被強制視為不匹配,但不會被移出陣列(見呼叫端說明)。
    """
    pc = _popcount_u64(hashes)
    return (pc < VIDEO_DEGENERATE_POPCOUNT) | (pc > 64 - VIDEO_DEGENERATE_POPCOUNT)


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
                pc = h.bit_count()
                if pc < IMAGE_DEGENERATE_POPCOUNT or pc > 64 - IMAGE_DEGENERATE_POPCOUNT:
                    # 退化指紋(近純色 / 近完美漸層):鑑別力太低,納入只會製造假群組,略過。
                    continue
                paths.append(entry.path)
                hashes.append(h)

    n = len(hashes)
    if n < 2:
        return []

    # 階段 2 前置:先把「完全相同的 dHash」收成同一桶,只對「相異指紋的代表」跑 O(m²) 錨點分群。
    # 完全重複的圖(同圖重存、重複下載)很常見,先摺疊可大幅縮小 m;桶內成員彼此距離必為 0,
    # 最後直接展開回原始索引。距離保證不變(代表跟錨點 <= threshold、桶內成員跟代表 = 0,故同組
    # 任兩張 <= threshold×2)。
    buckets: dict[int, list[int]] = {}
    for idx, h in enumerate(hashes):
        buckets.setdefault(h, []).append(idx)

    uniq = list(buckets.keys())
    m = len(uniq)
    arr = np.array(uniq, dtype=np.uint64)
    assigned = np.zeros(m, dtype=bool)
    uniq_groups: list[list[int]] = []   # 每組:uniq 的索引清單
    last_emit = 0.0
    for i in range(m):
        if cancelled():
            return []
        now = time.monotonic()
        if progress_cb and (i % 10 == 0 or (now - last_emit) >= PROGRESS_TIME_INTERVAL):
            last_emit = now
            progress_cb(2, i, m, paths[buckets[uniq[i]][0]])
        if assigned[i]:
            continue
        assigned[i] = True
        members = [i]
        if i + 1 < m:
            later_mask = np.zeros(m, dtype=bool)
            later_mask[i + 1 :] = ~assigned[i + 1 :]
            later_idx = np.nonzero(later_mask)[0]
            if later_idx.size:
                dists = _popcount_u64(arr[later_idx] ^ arr[i])
                matched = later_idx[dists <= threshold]
                if matched.size:
                    assigned[matched] = True
                    members.extend(matched.tolist())
        uniq_groups.append(members)

    # 展開回原始路徑索引:一組的成員 = 該組每個 uniq 指紋各自桶內的所有原始索引。
    result: list[list[str]] = []
    for members in uniq_groups:
        orig: list[int] = []
        for u in members:
            orig.extend(buckets[uniq[u]])
        if len(orig) >= 2:
            result.append([paths[k] for k in orig])
    return result


# ----------------------------------------------------------------------
# 影片相似:Smith-Waterman 區域比對
# ----------------------------------------------------------------------


def _match_matrix(
    a: np.ndarray,
    b: np.ndarray,
    frame_threshold: int,
    deg_a: np.ndarray | None = None,
    deg_b: np.ndarray | None = None,
) -> np.ndarray:
    """回傳 na×nb 布林矩陣:a[i] 與 b[j] 的 Hamming <= frame_threshold(視為同一畫面)。
    deg_a/deg_b(見 VIDEO_DEGENERATE_POPCOUNT):任一邊是退化幀的格子強制設為不匹配,
    避免黑畫面/純色轉場這類低鑑別力畫面互相誤判成「同一畫面」。
    """
    xor = a[:, None] ^ b[None, :]
    match = _popcount_u64(xor) <= frame_threshold
    if deg_a is not None:
        match &= ~deg_a[:, None]
    if deg_b is not None:
        match &= ~deg_b[None, :]
    return match


def _local_align(match_rows, na: int, nb: int):
    """對布林 match 矩陣(list[list[bool]])跑 Smith-Waterman 區域比對。
    回傳 (best_score, a_start, a_end, b_start, b_end, match_count)(索引皆 0-based、含端點)。

    match_count 是最佳區段回溯路徑上「真正對到的幀數」(對角線且該格為 match 的步數),
    用來計算實際相似秒數:對齊出來的區段裡可能夾雜 mismatch/gap,用區段跨度當相似時間會高估,
    改用真正對到的幀數才能過濾「跨度長但實際很多錯配」的巧合匹配。
    """
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
        return 0, 0, 0, 0, 0, 0
    # 回溯到 0,順便數對角線 match 步數
    i, j = bi, bj
    match_count = 0
    while i > 0 and j > 0 and H[i][j] > 0:
        cur = H[i][j]
        is_match = match_rows[i - 1][j - 1]
        s = _SW_MATCH if is_match else _SW_MISMATCH
        if cur == H[i - 1][j - 1] + s:
            if is_match:
                match_count += 1
            i -= 1
            j -= 1
        elif cur == H[i - 1][j] + _SW_GAP:
            i -= 1
        else:
            j -= 1
    return best, i, bi - 1, j, bj - 1, match_count


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
    cancel_check=None,
):
    """精修階段:粗篩只給了「大概哪一段對得上」的候選時間窗(可能因為粗篩間隔較粗而不準)。
    這裡只針對候選窗附近(留一點 margin 免得邊界剛好切到)重新密集取樣,重新跑一次
    Smith-Waterman,拿到精確到秒的邊界,同時二次驗證排除粗篩階段的巧合匹配。
    回傳 (a_start,a_end,b_start,b_end,matched_seconds) 或 None(驗證失敗)。

    重要:候選窗長度沒有先天上限——兩部都是長片、offset≈0 時,窗長會逼近整片長度。若仍固定用
    base_interval 密集取樣,解碼次數(每秒一次 seek)與 _local_align 的 O(na*nb) DP 都會隨窗長
    無上限爆掉(兩部 60 分鐘影片 = 3600×3600 純 Python DP + 數千次 seek)。因此取樣間隔依窗長
    自動放寬,確保兩邊各自的取樣數都 <= VIDEO_REFINE_MAX_SAMPLES;窗短時仍維持 base_interval 的
    秒級精度,長窗則退到幾秒精度(對「顯示相似區間給人看」完全夠用)。
    """
    margin = base_interval * 4
    a_start, a_end = max(0.0, ta0 - margin), ta1 + margin
    b_start, b_end = max(0.0, tb0 - margin), tb1 + margin

    span = max(a_end - a_start, b_end - b_start)
    interval = max(base_interval, span / VIDEO_REFINE_MAX_SAMPLES)

    hash_a = _sample_window(path_a, a_start, a_end, interval, VIDEO_REFINE_MAX_SAMPLES, cancel_check)
    hash_b = _sample_window(path_b, b_start, b_end, interval, VIDEO_REFINE_MAX_SAMPLES, cancel_check)
    if len(hash_a) < 2 or len(hash_b) < 2:
        return None

    arr_a = np.array(hash_a, dtype=np.uint64)
    arr_b = np.array(hash_b, dtype=np.uint64)
    match = _match_matrix(arr_a, arr_b, frame_threshold, _degenerate_mask(arr_a), _degenerate_mask(arr_b))
    best, a0, a1, b0, b1, match_count = _local_align(match.tolist(), len(hash_a), len(hash_b))
    if best <= 0:
        return None
    matched_sec = match_count * interval
    return (
        a_start + a0 * interval,
        a_start + (a1 + 1) * interval,
        b_start + b0 * interval,
        b_start + (b1 + 1) * interval,
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
    group_b=None,
    cache=None,
):
    """回傳 list[dict],每組:{"paths": [...], "segments": [人類可讀的相似片段字串, ...]}。
    min_match_seconds:最短連續相似片段(秒),過濾黑畫面/共用片頭之類的巧合匹配。
    fingerprint_workers:階段 1 平行算指紋的執行緒數(cv2 解碼會釋放 GIL,執行緒能真的平行跑)。

    cache:選填的 print_cache.PrintCache。給定時,沒動過的影片直接讀快取、完全不解碼
    (見 print_cache 模組說明)。**這個物件只會在呼叫端這條執行緒上被碰**——查快取在丟給
    ThreadPoolExecutor 之前做完,寫回也只在 as_completed 迴圈裡做,sqlite 連線不跨執行緒。
    None 時完全不快取(純函式行為,測試用)。

    group_b:給定時進入「資料夾對資料夾」模式——targets 視為群組 A、group_b 視為群組 B,
    最終結果只保留跨群組的配對(A 跟 A 自己、B 跟 B 自己都不比對)。預設 None 時完全不影響
    行為,等同改動前的「全部互比」。這只是縮小比對範圍,不是另一套演算法——省下多少視兩邊
    資料夾大小分布而定,若其中一邊夾了大量彼此相似的檔案,組內比對本來就會被錨點分群提早
    跳過,縮小範圍的效益會相對有限。

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

    t_phase1 = time.monotonic()

    # 階段 1a:先蒐集所有影片路徑(快,不必平行化),這樣才能事先知道總數給進度條用。
    # group_b 模式下,path_group 記錄每個路徑屬於 A 或 B;若兩組資料夾有重疊/巢狀(同一個檔案
    # 兩邊都掃得到),以先蒐集到的 A 為準、B 端跳過,避免同一部影片被重複算指紋、跟自己比對出
    # 一組假重複。
    paths = []
    path_group: dict[str, str] = {}
    for root in targets:
        for entry in safe_walk(root, exclude_dirs=EXCLUDED_DIRS):
            if cancelled():
                return []
            if os.path.splitext(entry.path)[1].lower() in VIDEO_EXTS and entry.path not in path_group:
                path_group[entry.path] = "A"
                paths.append(entry.path)
    if group_b is not None:
        for root in group_b:
            for entry in safe_walk(root, exclude_dirs=EXCLUDED_DIRS):
                if cancelled():
                    return []
                if os.path.splitext(entry.path)[1].lower() in VIDEO_EXTS and entry.path not in path_group:
                    path_group[entry.path] = "B"
                    paths.append(entry.path)

    videos = []  # list[dict]:指紋(順序不保證跟 paths 一致,分群不受影響)
    total = len(paths)
    scanned = 0
    last_emit = 0.0

    last_log = 0.0

    def _emit_phase1(path: str) -> None:
        nonlocal last_emit, last_log
        now = time.monotonic()
        if progress_cb and now - last_emit >= PROGRESS_TIME_INTERVAL:
            last_emit = now
            progress_cb(1, scanned, total, path)
        # 心跳:階段 1 在大型影片庫上要跑上一小時,中途沒有任何 log 的話,想診斷「現在到底
        # 卡在哪、有沒有在動」只能用猜的。
        if now - last_log >= PHASE2_LOG_INTERVAL:
            last_log = now
            el = now - t_phase1
            rate = scanned / el if el > 0 else 0.0
            left = (total - scanned) / rate / 60 if rate > 0 else 0.0
            logger.info(
                "階段 1(進行中):%.1f 分鐘 | 已處理 %d/%d 部(%.0f 部/秒,推估剩 %.0f 分鐘)",
                el / 60, scanned, total, rate, left,
            )

    def _accept(vp: dict, path: str) -> None:
        """指紋(不管來自快取或現算)通過時長門檻就收進 videos。"""
        if vp["duration"] < min_match_seconds:
            return
        vp["path"] = path            # 快取裡的 last_path 可能是這個檔案的舊位置,一律以現在的為準
        vp["group"] = path_group[path]
        vp.setdefault("degenerate", _degenerate_mask(vp["hashes"]))
        videos.append(vp)

    # 階段 1b:先查快取(協調執行緒、順序 I/O:每檔只 stat + 讀頭尾各 64KB,比解碼便宜好幾個
    # 數量級)。命中的直接用,只有 miss 才需要真的解碼。
    miss_paths = []
    miss_keys = {}
    for p in paths:
        if cancelled():
            return []
        key = print_cache.file_key(p) if cache is not None else None
        vp = cache.lookup(key, base_interval, max_samples) if cache is not None else None
        if vp is not None:
            # 只有命中才算「這部處理完了」。miss 的還要解碼,現在就記成已完成的話,進度條會在
            # 查快取階段就衝到 100%,然後在真正耗時的解碼階段整段卡在滿格不動。
            scanned += 1
            _accept(vp, p)
        else:
            miss_paths.append(p)
            miss_keys[p] = key
        _emit_phase1(p)   # 即使數字沒動也要發:UI 的路徑標籤會跟著跳,使用者才知道沒當掉

    # 階段 1c:平行算 miss 的粗篩指紋(cv2 的解碼呼叫會釋放 GIL,實測確實有平行加速)。
    # worker 數偏保守——傳統硬碟上開太多平行 seek 反而會互相拖累。
    if miss_paths:
        with ThreadPoolExecutor(max_workers=fingerprint_workers) as executor:
            futures = {
                executor.submit(
                    build_video_print, p, base_interval, max_samples, cancel_check, VIDEO_MIN_CACHEABLE_DURATION
                ): p
                for p in miss_paths
            }
            for fut in as_completed(futures):
                if cancelled():
                    executor.shutdown(wait=False, cancel_futures=True)
                    if cache is not None:
                        cache.flush()   # 已經算好的別浪費,下次掃描還能用
                    return []
                path = futures[fut]
                vp = fut.result()
                scanned += 1
                _emit_phase1(path)
                if vp is not None:
                    # 存快取要在時長門檻之前——min_match_seconds 是 UI 可調的,指紋本身跟它無關,
                    # 依它篩選過的快取會讓使用者改嚴格程度後莫名其妙 miss 一整輪。
                    if cache is not None:
                        cache.store(miss_keys[path], vp, base_interval, max_samples)
                    _accept(vp, path)
        if cache is not None:
            cache.flush()

    phase1_sec = time.monotonic() - t_phase1
    cache_note = ""
    if cache is not None:
        st = cache.stats()
        cache_note = f" | 快取命中 {st['hits']}、未命中 {st['misses']}"
    logger.info(
        "階段 1(指紋)完成:%.1f 秒 | 影片檔 %d 個 → 可用指紋 %d 份(%d 個解不開或太短)%s"
        " | 解碼 %d 執行緒(NEONSWEEP_FP_WORKERS 可調)、需解碼 %d 部",
        phase1_sec,
        total,
        len(videos),
        total - len(videos),
        cache_note,
        fingerprint_workers,
        len(miss_paths),
    )

    n = len(videos)
    if n < 2:
        return []

    # 階段 2:錨點分群(不用 Union-Find)。每部影片只跟「錨點」比對,匹配上就收進錨點的組、
    # 標記為已分組,之後不會再被其他錨點搶走、也不會再去測試別人。這避免了 Union-Find 遞移
    # 合併的長鏈問題(A 跟 B 像、B 跟 C 像,但 A、C 完全不像,三個卻被焊進同一組)——圖片那邊
    # 已經證實過會發生(見 find_similar_images 開頭說明),影片理論上一樣有風險,一併修掉。
    # 附帶好處:已分組的影片會被整個跳過,不會再被拿去跟後面的錨點比對,省下不少昂貴的
    # 位移投票/精修運算。

    # both_fine 分支進 DP 前的安全過濾門檻:Smith-Waterman 回溯路徑上「真正對到的幀數」
    # (match_count,見 _local_align)不可能超過 match 矩陣裡 True 的總格數——因為每一步匹配
    # 都消耗一個獨立的 True 格子。所以只要 match.sum() 湊不到「要達到 min_match_seconds
    # 所需的最少幀數」,就代表這對影片無論怎麼跑 DP 都不可能通過門檻,可以直接跳過整個
    # O(na*nb) 的純 Python DP。這個過濾在數學上不會漏掉任何原本能通過門檻的組合。
    required_match_frames = max(1, math.ceil(min_match_seconds / base_interval))

    assigned = [False] * n
    # 進度條分母(只求大致,不追求精確):cross 模式下同群組配對會在計數前就被跳過,
    # 若仍用 n(n-1)/2 會嚴重高估總量(A、B 各半時進度只會走到一半),改用 nA×nB。
    if group_b is not None:
        n_a = sum(1 for v in videos if v["group"] == "A")
        total_pairs_estimate = n_a * (n - n_a)
    else:
        total_pairs_estimate = n * (n - 1) // 2
    done_pairs = 0
    last_emit = 0.0
    result = []
    # 效能儀表(Stage 0):這些計數只餵 logger,不影響任何判斷。dp_calls/refine_calls 是
    # 階段 2 的成本大頭,pruned_pairs 則反映退化幀過濾 + match.sum() 剪枝擋掉多少昂貴路徑。
    t_phase2 = time.monotonic()
    dp_calls = 0
    refine_calls = 0
    pruned_pairs = 0
    last_log = time.monotonic()

    def _phase2_line(tag: str) -> str:
        el = time.monotonic() - t_phase2
        rate = done_pairs / el if el > 0 else 0.0
        left = (total_pairs_estimate - done_pairs) / rate / 60 if rate > 0 else 0.0
        return (
            f"階段 2({tag}):{el / 60:.1f} 分鐘 | 已比對 {done_pairs}/{total_pairs_estimate} 對"
            f"({100 * done_pairs / max(total_pairs_estimate, 1):.1f}%,{rate:.0f} 對/秒,推估剩 {left:.0f} 分鐘)"
            f" | 剪枝擋掉 {pruned_pairs}、進 DP {dp_calls} 次、進精修 {refine_calls} 次 | 目前 {len(result)} 組"
        )

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
            vb = videos[j]
            if group_b is not None and va["group"] == vb["group"]:
                # 資料夾對資料夾模式:同群組(都是 A 或都是 B)的配對不算數,跳過——比 both_fine/
                # 位移投票都便宜,擺在最前面先過濾掉。
                continue
            if cancelled():
                # 內層每對都檢查:一對比對可能觸發精修(解碼兩段影片,分鐘等級),不能只在
                # 錨點 i 的開頭檢查,否則按了取消還要等整輪候選跑完。
                logger.info(_phase2_line("已取消"))
                return []
            done_pairs += 1
            now = time.monotonic()
            if progress_cb and now - last_emit >= PROGRESS_TIME_INTERVAL:
                last_emit = now
                progress_cb(2, done_pairs, total_pairs_estimate, va["path"])
            if now - last_log >= PHASE2_LOG_INTERVAL:
                last_log = now
                logger.info(_phase2_line("進行中"))

            if va["hashes"].shape == vb["hashes"].shape and np.array_equal(va["hashes"], vb["hashes"]):
                # 指紋陣列逐 bit 相同:內容完全一致(常見於同一部影片重複下載、只是容器/編碼
                # 參數不同),兩邊 Hamming 距離處處為 0,不必再跑 _match_matrix/_local_align 或
                # 位移投票就已經知道整段相符。比照圖片那邊「完全相同先摺疊」的精神,但這裡不用
                # 預先分桶——單純在配對當下用一次陣列比較短路掉本來要跑的 DP/位移投票,實作
                # 更簡單、正確性顯而易見(陣列相同 ⇒ 距離必為 0,不會有假陽性)。
                dup_dur = min(va["duration"], vb["duration"])
                if dup_dur < min_match_seconds:
                    pruned_pairs += 1
                    continue
                assigned[j] = True
                members.append(j)
                segments.append(
                    f"{os.path.basename(va['path'])} {_fmt_ts(0.0)}–{_fmt_ts(va['duration'])}"
                    f"  ≈  {os.path.basename(vb['path'])} {_fmt_ts(0.0)}–{_fmt_ts(vb['duration'])}"
                )
                continue

            both_fine = va["interval"] <= base_interval * 1.0001 and vb["interval"] <= base_interval * 1.0001

            if both_fine:
                # 兩邊都還沒被放寬(短片對短片),原生指紋本來就是同一個間隔,兩邊取樣起點都是
                # 各自的 t=0、相位天生一致,直接對齊即可,不必再解一次影片。
                match = _match_matrix(va["hashes"], vb["hashes"], frame_threshold, va["degenerate"], vb["degenerate"])
                if int(match.sum()) < required_match_frames:
                    pruned_pairs += 1
                    continue
                dp_calls += 1
                best, a0, a1, b0, b1, match_count = _local_align(
                    match.tolist(), len(va["hashes"]), len(vb["hashes"])
                )
                if best <= 0:
                    continue
                matched_sec = match_count * base_interval
                if matched_sec < min_match_seconds:
                    continue
                fa0, fa1 = a0 * base_interval, (a1 + 1) * base_interval
                fb0, fb1 = b0 * base_interval, (b1 + 1) * base_interval
            else:
                # 至少一邊牽涉到長片(間隔被放寬過):兩邊取樣的相位不保證對齊,改用位移投票
                # 找出候選重疊窗,再把 B 的全長投影到 A 的時間軸上,只在那個窗附近精修。
                offset, votes, hit_span = _estimate_offset(va, vb, frame_threshold)
                if offset is None or votes < 2:
                    pruned_pairs += 1
                    continue
                ta0 = max(0.0, offset)
                ta1 = min(va["duration"], offset + vb["duration"])
                if ta1 - ta0 < min_match_seconds:
                    pruned_pairs += 1
                    continue
                # 把候選窗從「幾何重疊」收窄到「投票證據實際落在的範圍」。這是階段 2 最大的
                # 成本來源:兩部 30 分鐘影片在 offset≈0 時,幾何重疊 = 整整 1800 秒,精修會對
                # 兩邊各取樣 VIDEO_REFINE_MAX_SAMPLES(600)次 = 1200 次 seek,傳統硬碟上光是
                # 磁頭移動就要 ~14 秒——而絕大多數會走到這裡的配對其實只是 2 票的巧合,根本不
                # 相似。收窄後,巧合配對的窗只有幾十秒(取樣數跟著掉一個數量級),真正的重疊
                # 則因為投票樣本本來就散佈在整段重疊上,窗幾乎不變、精度不受影響。
                #
                # 為什麼不會漏抓:真的有 T 秒重疊時,粗篩會在整段 T 上取到 ~T/bucket 個樣本、
                # 全部投給同一個位移,所以 hit_span 本來就涵蓋整段重疊。再往外墊
                # min_match_seconds,讓「重疊比證據稍微長一點」的邊緣情況也有餘裕。
                lo, hi = hit_span
                pad = min_match_seconds + max(va["interval"], vb["interval"])
                ta0 = max(ta0, lo - pad)
                ta1 = min(ta1, hi + pad)
                if ta1 - ta0 < min_match_seconds:
                    pruned_pairs += 1
                    continue
                tb0, tb1 = ta0 - offset, ta1 - offset
                refine_calls += 1
                refined = _refine_match(
                    va["path"], vb["path"], ta0, ta1, tb0, tb1, frame_threshold, base_interval, cancel_check
                )
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

    phase2_sec = time.monotonic() - t_phase2
    logger.info(
        "階段 2(比對)完成:%.1f 秒 | 影片 %d 部、實際比對 %d 對(估計上限 %d)| "
        "便宜剪枝擋掉 %d 對、進 DP %d 次、進精修 %d 次 | 產出 %d 組",
        phase2_sec,
        n,
        done_pairs,
        total_pairs_estimate,
        pruned_pairs,
        dp_calls,
        refine_calls,
        len(result),
    )
    logger.info("相似影片掃描總計:%.1f 秒(階段 1:%.1f 秒、階段 2:%.1f 秒)", phase1_sec + phase2_sec, phase1_sec, phase2_sec)

    return result
