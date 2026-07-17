"""影片粗篩指紋的 SQLite 快取(純邏輯層,不碰 Qt,比照 similarity.py)。

為什麼要有這個:算一部影片的粗篩指紋要 seek + 解碼最多 VIDEO_MAX_SAMPLES 次,傳統硬碟上
這是整個相似影片掃描最貴的一段(數千部影片 = 數小時)。但影片庫大部分檔案在兩次掃描之間
根本沒動過,重算完全是浪費。

**快取 key 刻意用「內容特徵」而不是路徑**:`(size, mtime_ns, quick_hash)`。
使用者整理影片庫時經常整個資料夾搬移/改名,若用路徑當 key,那一整批就會全部 miss、
重新解碼一輪——那等於沒有快取。改用內容特徵後,檔案搬到哪裡(甚至跨磁碟)都還是命中;
反過來,內容真的被改寫(重新轉檔、剪輯)會讓 mtime 或頭尾雜湊變動,自然失效重算。

`quick_hash` 只讀頭尾各 64KB(順序 I/O,幾毫秒),不是全檔雜湊——它的作用不是證明兩個
檔案相同,而是把「size 和 mtime 剛好都一樣」的碰撞機率壓到可忽略(批次下載/轉檔產生的
檔案有機會 size+mtime 相同)。真正的內容比對本來就由 dHash 指紋負責。
"""

import hashlib
import logging
import os
import sqlite3
import time

import numpy as np

from .utils.fs import long_path, user_data_dir

logger = logging.getLogger(__name__)

# 指紋演算法版本:取樣邏輯、裁邊參數(WATERMARK_CROP_MARGIN)、dHash 演算法等有「語意」
# 變更時 +1,舊資料就會自動失效(查詢時 fp_version 不符視為 miss)。純效能改動(例如換
# 解碼後端但取樣點與雜湊結果不變)不需要動這個。
FP_VERSION = 1

QUICK_HASH_CHUNK = 64 * 1024   # 頭尾各讀這麼多算 quick_hash
_COMMIT_BATCH = 50             # 每累積幾筆 commit 一次(逐筆 commit 在 HDD 上很慢)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prints (
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    quick_hash    BLOB    NOT NULL,
    fp_version    INTEGER NOT NULL,
    base_interval REAL    NOT NULL,
    max_samples   INTEGER NOT NULL,
    duration      REAL    NOT NULL,
    interval      REAL    NOT NULL,
    hashes        BLOB    NOT NULL,
    times         BLOB    NOT NULL,
    backend       TEXT    NOT NULL,
    last_path     TEXT,
    last_seen     INTEGER,
    PRIMARY KEY (size, mtime_ns, quick_hash, fp_version, base_interval, max_samples)
);

CREATE TABLE IF NOT EXISTS pairs (
    key_a             BLOB    NOT NULL,
    key_b             BLOB    NOT NULL,
    fp_version        INTEGER NOT NULL,
    base_interval     REAL    NOT NULL,
    max_samples       INTEGER NOT NULL,
    frame_threshold   INTEGER NOT NULL,
    min_match_seconds REAL    NOT NULL,
    matched           INTEGER NOT NULL,
    a_start REAL, a_end REAL, b_start REAL, b_end REAL,
    PRIMARY KEY (key_a, key_b, fp_version, base_interval, max_samples,
                 frame_threshold, min_match_seconds)
);
"""


def file_key(path: str) -> tuple[int, int, bytes] | None:
    """算 (size, mtime_ns, quick_hash);讀不到檔案回傳 None(呼叫端當 miss 處理)。

    size 也餵進雜湊,讓「頭尾相同但中間長度不同」的檔案不會撞在一起。
    """
    try:
        p = long_path(path)
        st = os.stat(p)
        size = st.st_size
        h = hashlib.blake2b(digest_size=16)
        h.update(size.to_bytes(8, "little"))
        with open(p, "rb") as fh:
            h.update(fh.read(QUICK_HASH_CHUNK))
            # 檔案小於等於兩個 chunk 時,上面那次讀已經涵蓋全檔,再 seek 讀尾巴只是重複
            if size > QUICK_HASH_CHUNK * 2:
                fh.seek(-QUICK_HASH_CHUNK, os.SEEK_END)
                h.update(fh.read(QUICK_HASH_CHUNK))
        return size, st.st_mtime_ns, h.digest()
    except OSError:
        return None


def content_id(key: tuple[int, int, bytes] | None) -> bytes | None:
    """把 file_key() 的 (size, mtime_ns, quick_hash) 壓成固定 16 bytes,給配對結論快取
    (見 pairs 表)當穩定識別碼——理由跟 file_key 本身一樣:不含路徑,搬移/改名不影響命中。
    key 為 None(讀不到檔案)時回傳 None,呼叫端據此判斷這個檔案不能參與配對快取。
    """
    if key is None:
        return None
    size, mtime_ns, quick = key
    h = hashlib.blake2b(digest_size=16)
    h.update(size.to_bytes(8, "little"))
    h.update(mtime_ns.to_bytes(8, "little", signed=True))
    h.update(quick)
    return h.digest()


class PrintCache:
    """指紋快取。**只能在單一執行緒(協調執行緒)使用**——sqlite 連線不跨執行緒共用,
    這也是 find_similar_videos 把「查快取」放在協調執行緒、只把 miss 丟給 ThreadPoolExecutor
    的原因。

    任何 DB 層面的失敗(損壞、鎖住、磁碟滿、目錄不可寫)一律降級成「這次不快取」,
    絕不讓掃描 crash——快取是純效能優化,壞掉最多是慢,不該影響功能。
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or os.path.join(user_data_dir(), "fingerprint_cache.db")
        self._conn: sqlite3.Connection | None = None
        self._pending = 0
        self._hits = 0
        self._misses = 0
        self._stores = 0
        self._open()

    # ---------------------------------------------------------------- 開關
    def _connect(self) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")      # 讀寫不互鎖
        conn.execute("PRAGMA synchronous=NORMAL")    # 快取資料掉了頂多重算,不必每次 fsync
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn

    def _open(self) -> None:
        try:
            self._connect()
            return
        except (sqlite3.Error, OSError) as e:
            logger.warning("指紋快取開啟失敗(%s),嘗試重建:%s", self._db_path, e)
        # 損壞或 schema 不相容:砍掉重建(裡面只有可重算的衍生資料,丟掉沒有任何損失)
        try:
            if self._conn is not None:
                self._conn.close()
        except sqlite3.Error:
            pass
        self._conn = None
        try:
            for suffix in ("", "-wal", "-shm"):
                stale = self._db_path + suffix
                if os.path.exists(stale):
                    os.remove(stale)
            self._connect()
            logger.info("指紋快取已重建:%s", self._db_path)
        except (sqlite3.Error, OSError) as e:
            logger.warning("指紋快取無法使用,本次掃描不快取:%s", e)
            self._conn = None

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.commit()
            self._conn.close()
        except sqlite3.Error:
            pass
        finally:
            self._conn = None

    # ---------------------------------------------------------------- 讀寫
    def lookup(self, key, base_interval: float, max_samples: int) -> dict | None:
        """命中回傳指紋 dict(不含 path/degenerate,由呼叫端補),否則 None。"""
        if self._conn is None or key is None:
            self._misses += 1
            return None
        size, mtime_ns, quick = key
        try:
            row = self._conn.execute(
                "SELECT duration, interval, hashes, times, backend FROM prints "
                "WHERE size=? AND mtime_ns=? AND quick_hash=? AND fp_version=? "
                "AND base_interval=? AND max_samples=?",
                (size, mtime_ns, quick, FP_VERSION, base_interval, max_samples),
            ).fetchone()
        except sqlite3.Error as e:
            logger.warning("指紋快取查詢失敗(當成 miss):%s", e)
            self._misses += 1
            return None
        if row is None:
            self._misses += 1
            return None

        duration, interval, hashes_blob, times_blob, backend = row
        self._hits += 1
        # frombuffer 給的是唯讀 view(直接指向 blob 記憶體),copy() 成正常可寫陣列;
        # dtype 寫死位元組序,不靠平台預設。
        return {
            "duration": duration,
            "interval": interval,
            "hashes": np.frombuffer(hashes_blob, dtype="<u8").copy(),
            "times": np.frombuffer(times_blob, dtype="<f8").copy(),
            "backend": backend,
        }

    def store(self, key, vp: dict, base_interval: float, max_samples: int) -> None:
        if self._conn is None or key is None:
            return
        size, mtime_ns, quick = key
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO prints (size, mtime_ns, quick_hash, fp_version, "
                "base_interval, max_samples, duration, interval, hashes, times, backend, "
                "last_path, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    size,
                    mtime_ns,
                    quick,
                    FP_VERSION,
                    base_interval,
                    max_samples,
                    float(vp["duration"]),
                    float(vp["interval"]),
                    vp["hashes"].astype("<u8").tobytes(),
                    vp["times"].astype("<f8").tobytes(),
                    vp["backend"],
                    vp.get("path"),
                    int(time.time()),
                ),
            )
            self._stores += 1
            self._pending += 1
            if self._pending >= _COMMIT_BATCH:
                self.flush()
        except (sqlite3.Error, KeyError) as e:
            logger.warning("指紋快取寫入失敗(跳過,不影響掃描):%s", e)

    # ---------------------------------------------------------------- 配對結論(見 pairs 表)
    def lookup_pair(
        self, id_a: bytes, id_b: bytes, base_interval: float, max_samples: int,
        frame_threshold: int, min_match_seconds: float,
    ) -> dict | None:
        """兩部影片(用 content_id() 算出的識別碼,呼叫端要先排序過,同一對不管誰先誰後
        都查到同一列)這次比對的結論。回傳 None = 沒紀錄(要重新比對);
        {"matched": False} = 之前比過、不相似;{"matched": True, "a_start"/"a_end"/
        "b_start"/"b_end": ...} = 之前比過、相似,附相似區間(各自時間軸,秒)。
        """
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT matched, a_start, a_end, b_start, b_end FROM pairs WHERE "
                "key_a=? AND key_b=? AND fp_version=? AND base_interval=? AND max_samples=? "
                "AND frame_threshold=? AND min_match_seconds=?",
                (id_a, id_b, FP_VERSION, base_interval, max_samples, frame_threshold, min_match_seconds),
            ).fetchone()
        except sqlite3.Error as e:
            logger.warning("配對結論快取查詢失敗(當成 miss):%s", e)
            return None
        if row is None:
            return None
        matched, a_start, a_end, b_start, b_end = row
        if not matched:
            return {"matched": False}
        return {"matched": True, "a_start": a_start, "a_end": a_end, "b_start": b_start, "b_end": b_end}

    def store_pair(
        self, id_a: bytes, id_b: bytes, base_interval: float, max_samples: int,
        frame_threshold: int, min_match_seconds: float, matched: bool,
        span: tuple[float, float, float, float] | None = None,
    ) -> None:
        if self._conn is None:
            return
        a_start = a_end = b_start = b_end = None
        if matched and span is not None:
            a_start, a_end, b_start, b_end = span
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO pairs (key_a, key_b, fp_version, base_interval, "
                "max_samples, frame_threshold, min_match_seconds, matched, a_start, a_end, "
                "b_start, b_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    id_a, id_b, FP_VERSION, base_interval, max_samples, frame_threshold,
                    min_match_seconds, 1 if matched else 0, a_start, a_end, b_start, b_end,
                ),
            )
            self._pending += 1
            if self._pending >= _COMMIT_BATCH:
                self.flush()
        except sqlite3.Error as e:
            logger.warning("配對結論快取寫入失敗(跳過,不影響掃描):%s", e)

    def flush(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.commit()
            self._pending = 0
        except sqlite3.Error as e:
            logger.warning("指紋快取 commit 失敗:%s", e)

    # ---------------------------------------------------------------- 儀表
    def stats(self) -> dict:
        return {"hits": self._hits, "misses": self._misses, "stores": self._stores}

    def reset_stats(self) -> None:
        self._hits = self._misses = self._stores = 0
