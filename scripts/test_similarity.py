"""相似影片偵測的驗收測試(開發期用,不進打包)。

執行:python scripts/test_similarity.py

用合成影片,不需要外部素材。合成手法:每個「內容秒」用同一張隨機雜訊幀重複 fps 次
——這樣不管取樣點落在該秒的哪個位置,拿到的畫面都一樣,dHash 才穩定可比。隨機雜訊的
dHash popcount 約 32(遠離退化門檻),黑/白幀則是刻意的退化幀。

刻意不用 pytest:專案沒有這個相依(NeonSweep.spec 的 excludes 還排除了它),
scripts/ 底下都是 `python scripts/xxx.py` 直接跑的獨立腳本,這裡比照辦理。
"""

import os
import shutil
import sys
import tempfile

# 讓 `python scripts/test_similarity.py` 能 import cleaner 套件
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")  # 開發機 console 是 cp950,中文/≈ 會炸

import cv2
import numpy as np

from cleaner import similarity as sim

FPS = 10
SIZE = 64

_results: list[tuple[str, bool, str]] = []


# ----------------------------------------------------------------------
# 合成影片工具
# ----------------------------------------------------------------------


def content_frames(seed: int, seconds: int, fps: int = FPS) -> list[np.ndarray]:
    """產生 `seconds` 秒的內容:每秒一張固定的隨機雜訊幀,重複 fps 次。"""
    rng = np.random.RandomState(seed)
    out = []
    for _ in range(seconds):
        frame = rng.randint(0, 255, (SIZE, SIZE, 3), dtype=np.uint8)
        out.extend([frame] * fps)
    return out


def solid_frames(value: int, seconds: int, fps: int = FPS) -> list[np.ndarray]:
    """純色幀(黑/白):dHash 退化,鑑別力為零。"""
    return [np.full((SIZE, SIZE, 3), value, dtype=np.uint8)] * (seconds * fps)


def write_video(path: str, frames: list[np.ndarray], fps: int = FPS, fourcc: str = "mp4v") -> str:
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), fps, (SIZE, SIZE))
    for f in frames:
        vw.write(f)
    vw.release()
    return path


def group_names(groups) -> list[list[str]]:
    return sorted(sorted(os.path.basename(p) for p in g["paths"]) for g in groups)


def check(name: str, passed: bool, detail: str = "") -> None:
    _results.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"\n        {detail}" if detail and not passed else ""))


# ----------------------------------------------------------------------
# 測試 1:黑幕誤判防護
# ----------------------------------------------------------------------


def test_black_frame_guard(tmp: str) -> None:
    """A(黑幕開場+內容X)、B(白幕開場+內容X)、C(黑幕開場+內容Y)
    → A+B 同組;C 不得因為跟 A 共享黑幕開場就被歸進去。
    """
    d = os.path.join(tmp, "black")
    os.makedirs(d)
    x = content_frames(seed=101, seconds=25)
    y = content_frames(seed=202, seconds=25)
    write_video(os.path.join(d, "A.mp4"), solid_frames(0, 5) + x)
    write_video(os.path.join(d, "B.mp4"), solid_frames(255, 3) + x)
    write_video(os.path.join(d, "C.mp4"), solid_frames(0, 5) + y)

    groups = group_names(sim.find_similar_videos([d], min_match_seconds=10))
    check("黑幕誤判防護:A+B 同組且 C 不入組", groups == [["A.mp4", "B.mp4"]], f"實際={groups}")


# ----------------------------------------------------------------------
# 測試 2:資料夾對資料夾(cross)模式
# ----------------------------------------------------------------------


def test_cross_mode(tmp: str) -> None:
    """群組 A 內部的重複不該報;跨 A×B 的重複要報;不帶 group_b 時行為不變。"""
    root = os.path.join(tmp, "cross")
    da, db = os.path.join(root, "A"), os.path.join(root, "B")
    os.makedirs(da)
    os.makedirs(db)
    shared = content_frames(seed=303, seconds=25)
    internal = content_frames(seed=404, seconds=25)
    write_video(os.path.join(da, "A1.mp4"), shared)
    write_video(os.path.join(da, "A2.mp4"), internal)
    write_video(os.path.join(da, "A3.mp4"), internal)   # A 內部重複
    write_video(os.path.join(db, "B1.mp4"), shared)     # 跨組重複

    cross = group_names(sim.find_similar_videos([da], min_match_seconds=10, group_b=[db]))
    check("cross 模式:只報跨組配對、不報 A 內部重複", cross == [["A1.mp4", "B1.mp4"]], f"實際={cross}")

    everything = group_names(sim.find_similar_videos([da, db], min_match_seconds=10))
    expected = [["A1.mp4", "B1.mp4"], ["A2.mp4", "A3.mp4"]]
    check("預設模式(不帶 group_b)行為不變", everything == expected, f"實際={everything}")


def test_cross_overlap_dedup(tmp: str) -> None:
    """兩組資料夾重疊時,同一個檔案不能被算兩次指紋、跟自己配成一組假重複。"""
    root = os.path.join(tmp, "overlap")
    sub = os.path.join(root, "sub")
    os.makedirs(sub)
    write_video(os.path.join(sub, "V1.mp4"), content_frames(seed=505, seconds=25))

    # A=root(含 sub)、B=sub:V1 兩邊都掃得到
    groups = group_names(sim.find_similar_videos([root], min_match_seconds=10, group_b=[sub]))
    check("cross 模式:重疊資料夾不產生自我配對假重複", groups == [], f"實際={groups}")


# ----------------------------------------------------------------------
# 測試 3:剪枝不變量(數學保證無假陰性)
# ----------------------------------------------------------------------


def test_pruning_invariant() -> None:
    """_local_align 回溯路徑的 match_count 不可能超過 match 矩陣的 True 總數
    ——這是 required_match_frames 便宜剪枝不會漏抓的依據。
    """
    rng = np.random.RandomState(42)
    bad = None
    for _ in range(500):
        na, nb = int(rng.randint(1, 16)), int(rng.randint(1, 16))
        density = float(rng.choice([0.05, 0.1, 0.3, 0.5, 0.8]))
        match = rng.rand(na, nb) < density
        _best, _a0, _a1, _b0, _b1, match_count = sim._local_align(match.tolist(), na, nb)
        if match_count > int(match.sum()):
            bad = (match_count, int(match.sum()))
            break
    check("剪枝不變量:match_count <= match.sum()(500 組隨機矩陣)", bad is None, f"反例={bad}")


# ----------------------------------------------------------------------
# 測試 4:重編碼的重複仍抓得到
# ----------------------------------------------------------------------


def test_reencode(tmp: str) -> None:
    """同一段內容用不同編碼參數(不同 fps + 不同 fourcc)產生,仍要分同組。
    Stage 2 換 PyAV 關鍵幀取樣後,兩邊關鍵幀位置不一致,這個測試是主要守門員。
    """
    d = os.path.join(tmp, "reencode")
    os.makedirs(d)
    frames_10 = content_frames(seed=606, seconds=25, fps=10)
    frames_15 = content_frames(seed=606, seconds=25, fps=15)  # 同樣的每秒內容,不同 fps
    write_video(os.path.join(d, "orig.mp4"), frames_10, fps=10, fourcc="mp4v")
    write_video(os.path.join(d, "reenc.avi"), frames_15, fps=15, fourcc="MJPG")

    groups = group_names(sim.find_similar_videos([d], min_match_seconds=10))
    check("重編碼(不同 fps/fourcc)的同內容仍分同組", groups == [["orig.mp4", "reenc.avi"]], f"實際={groups}")


# ----------------------------------------------------------------------
# 測試 5:長片涵蓋(DEVDOC §11 既有驗收)
# ----------------------------------------------------------------------


def test_long_video_coverage(tmp: str) -> None:
    """長片(> base_interval×max_samples = 300 秒,取樣間隔會被自動放寬)中後段剪出來的
    短片,仍要被偵測到——驗證粗篩涵蓋全片 + 位移投票 + 精修這條路徑。
    """
    d = os.path.join(tmp, "long")
    os.makedirs(d)
    full = content_frames(seed=707, seconds=400)          # 400 秒 → interval 放寬到 1.33s
    clip = full[350 * FPS : 385 * FPS]                    # 從第 350 秒剪 35 秒出來
    write_video(os.path.join(d, "full.mp4"), full)
    write_video(os.path.join(d, "clip.mp4"), clip)

    groups = group_names(sim.find_similar_videos([d], min_match_seconds=20))
    check("長片中後段剪出的短片仍偵測得到", groups == [["clip.mp4", "full.mp4"]], f"實際={groups}")


# ----------------------------------------------------------------------
# 測試 6:精修窗收窄(階段 2 最大的成本來源,回歸防護)
# ----------------------------------------------------------------------


def test_refine_window_bounded(tmp: str) -> None:
    """兩部長片只共用一小段(短於 min_match_seconds,不算重複),但足以在粗篩湊到 >=2 票
    而觸發精修——真實影片庫裡最常見的誤觸發樣態(共用片頭、相似轉場)。

    精修的取樣次數必須跟「證據範圍」成正比,而不是跟「幾何重疊」成正比。收窄前這裡會對
    兩邊各取樣 400 次(共 800 次 seek,HDD 上 ~9.6 秒);收窄後只需要 ~116 次。
    這個上限守不住的話,大型影片庫的掃描時間會回到「一整天」的等級。
    """
    d = os.path.join(tmp, "refine")
    os.makedirs(d)
    shared = content_frames(seed=999, seconds=8)          # 只共用 8 秒 < min_match_seconds
    write_video(os.path.join(d, "A.mp4"), content_frames(1, 196) + shared + content_frames(2, 196))
    write_video(os.path.join(d, "B.mp4"), content_frames(3, 196) + shared + content_frames(4, 196))

    orig = sim._sample_window
    counted = {"samples": 0}

    def counting(path, start, end, interval, max_samples=None, cancel_check=None):
        out = orig(path, start, end, interval, max_samples, cancel_check)
        counted["samples"] += len(out)
        return out

    sim._sample_window = counting
    try:
        groups = group_names(sim.find_similar_videos([d], min_match_seconds=20))
    finally:
        sim._sample_window = orig

    check("精修:只共用 8 秒的長片不該被判為重複", groups == [], f"實際={groups}")
    check(
        "精修:候選窗收窄到證據範圍(取樣數 << 幾何重疊)",
        counted["samples"] < 300,
        f"取樣 {counted['samples']} 次(收窄前為 800;超過 300 代表收窄失效)",
    )


# ----------------------------------------------------------------------
# 測試 7:取消回應性(build_video_print 內層要檢查取消旗標)
# ----------------------------------------------------------------------


def test_cancel_responsive(tmp: str) -> None:
    """使用者實測回報:按下取消要多按幾次、等一陣子才生效。根因是 build_video_print 的
    取樣迴圈完全不檢查 cancel_check——已經在解的影片會解完全部 max_samples(最多 300 次
    seek)才停,不是這裡加了 cancel_check 就能馬上生效,而是「最多卡一次取樣的時間」而不是
    「最多卡整部影片解完的時間」。

    用計數式 cancel_check 直接驗證:影片有足夠多可取樣的秒數(遠超過 K),cancel_check 在
    第 K 次呼叫時才回傳 True,呼叫次數必須剛好停在 K——如果迴圈沒有檢查旗標,呼叫次數會
    一路衝到底(全部樣本數),不會停在 K。這個判準不靠計時(避免環境速度不同造成誤判),
    直接測「有沒有真的檢查」。
    """
    d = os.path.join(tmp, "cancel")
    os.makedirs(d)
    # 100 秒、每秒可取一個樣本,遠超過下面的 K=5,才能區分「有停」跟「沒停一路衝到底」。
    path = write_video(os.path.join(d, "long.mp4"), content_frames(seed=1, seconds=100))

    K = 5
    calls = {"n": 0}

    def cancel_after_k():
        calls["n"] += 1
        return calls["n"] >= K

    result = sim.build_video_print(path, cancel_check=cancel_after_k)

    check("取消:cancel_check 觸發時立刻回傳 None(不回傳部分指紋)", result is None,
          f"實際={type(result).__name__}")
    check(
        "取消:迴圈在第 K 次檢查就停,不會解完全部樣本",
        calls["n"] == K,
        f"cancel_check 被呼叫 {calls['n']} 次(應為 {K};遠大於 {K} 代表迴圈沒真的檢查旗標)",
    )


# ----------------------------------------------------------------------
# 測試 8:階段 1 進度條(快取讓這條容易出錯)
# ----------------------------------------------------------------------


def test_phase1_progress(tmp: str) -> None:
    """進度回報的 done 只能算「真的處理完的影片」。

    踩過的雷:加了快取之後,查快取的迴圈把 done 一路加到 total,但真正的解碼在那之後才開始
    ——進度條會先衝到 100%,然後在最耗時的解碼階段整段卡在滿格。結果完全正確,所以測試分群
    的案例抓不到,只有使用者會看到「卡住的 100%」。
    """
    d = os.path.join(tmp, "prog")
    os.makedirs(d)
    for i in range(4):
        write_video(os.path.join(d, f"p{i}.mp4"), content_frames(seed=700 + i, seconds=25))

    seen: list[tuple[int, int]] = []
    sim.find_similar_videos(
        [d], min_match_seconds=10,
        progress_cb=lambda phase, done, total, path: seen.append((done, total)) if phase == 1 else None,
    )
    check("階段 1 進度:done 不超過 total", all(d_ <= t_ for d_, t_ in seen), f"越界樣本={[x for x in seen if x[0] > x[1]][:3]}")

    # 這裡沒給 cache,所以每一部都是 miss、都還要解碼——**第一次回報時 done 必須是 0**。
    # 這正是抓那個 bug 的判準:當時查快取的迴圈對每個路徑都 scanned += 1,所以第一次回報就會
    # 是 done=1,查完 4 部就直接 4/4 滿格,而解碼一幀都還沒開始。
    check("階段 1 進度:解碼開始前不得回報任何「已完成」", bool(seen) and seen[0][0] == 0,
          f"首次回報={seen[0] if seen else '(完全沒回報)'},應為 (0, 4)")


# ----------------------------------------------------------------------
# 測試 8b:指紋陣列完全相同的快速通道(逐 bit 相同,免走 DP/位移投票)
# ----------------------------------------------------------------------


def test_exact_duplicate_fastpath(tmp: str) -> None:
    """兩部影片指紋陣列逐 bit 相同(同內容、同 fps/長度,只是容器不同)時,應直接判定整段
    相符,不必落到 both_fine 的 DP 或位移投票路徑——這裡驗證的是「結果仍正確」,不是走了
    哪條程式碼路徑(白箱驗證留給人工讀 code,測試只看得到黑箱行為)。
    """
    d = os.path.join(tmp, "exact_dup")
    os.makedirs(d)
    frames = content_frames(seed=12345, seconds=30)
    write_video(os.path.join(d, "orig.mp4"), frames, fourcc="mp4v")
    write_video(os.path.join(d, "dup.avi"), frames, fourcc="MJPG")

    groups = sim.find_similar_videos([d], min_match_seconds=10)
    names = group_names(groups)
    check("完全相同指紋:仍歸同組", names == [["dup.avi", "orig.mp4"]], f"實際={names}")
    if groups:
        seg = groups[0]["segments"][0]
        check("完全相同指紋:相似片段涵蓋全片(非部分窗)", "00:00" in seg and "00:30" in seg, f"實際={seg}")


# ----------------------------------------------------------------------
# 測試 9:掃描期間的程序優先權
# ----------------------------------------------------------------------


def test_background_priority() -> None:
    """降優先權失敗是**無聲**的(ctypes 不會丟例外,只是什麼都沒發生),所以要真的去讀回來確認。

    踩過的雷:沒宣告 restype 時,GetCurrentProcess() 的 64 位元 pseudo-handle 會被截成 32 位元,
    SetPriorityClass 收到無效 handle 直接失敗——掃描照跑、使用者照卡,但沒有任何錯誤訊息。
    """
    from cleaner.utils.proc import (
        BELOW_NORMAL_PRIORITY_CLASS,
        NORMAL_PRIORITY_CLASS,
        BackgroundPriority,
        current_priority_class,
    )

    before = current_priority_class()
    with BackgroundPriority():
        inside = current_priority_class()
    after = current_priority_class()

    check("優先權:區間內確實降到 below-normal", inside == BELOW_NORMAL_PRIORITY_CLASS,
          f"區間內={hex(inside)},應為 {hex(BELOW_NORMAL_PRIORITY_CLASS)}(0 代表 API 呼叫失敗)")
    check("優先權:離開區間後還原成 normal", after == NORMAL_PRIORITY_CLASS,
          f"離開後={hex(after)}(進入前={hex(before)})")


# ----------------------------------------------------------------------
# 測試 10:指紋快取(Stage 1 起;未實作時自動略過)
# ----------------------------------------------------------------------


def test_cache(tmp: str) -> None:
    try:
        from cleaner import print_cache
    except ImportError:
        print("  SKIP  指紋快取(Stage 1 尚未實作)")
        return

    d = os.path.join(tmp, "cache")
    os.makedirs(d)
    v1 = write_video(os.path.join(d, "c1.mp4"), content_frames(seed=808, seconds=25))
    write_video(os.path.join(d, "c2.mp4"), content_frames(seed=909, seconds=25))

    db = os.path.join(tmp, "cache_test.db")
    cache = print_cache.PrintCache(db)
    try:
        # 第一次:全 miss,指紋寫進 DB
        sim.find_similar_videos([d], min_match_seconds=10, cache=cache)
        stats1 = cache.stats()
        check("快取:首次掃描全部 miss", stats1["misses"] == 2 and stats1["hits"] == 0, f"實際={stats1}")

        # 第二次:同樣的檔案應該全 hit
        cache.reset_stats()
        sim.find_similar_videos([d], min_match_seconds=10, cache=cache)
        stats2 = cache.stats()
        check("快取:重掃全部命中(不重算指紋)", stats2["hits"] == 2 and stats2["misses"] == 0, f"實際={stats2}")

        # 搬移 + 改名後仍要命中(key 不含路徑)
        moved = os.path.join(d, "renamed_subdir")
        os.makedirs(moved)
        shutil.move(v1, os.path.join(moved, "renamed.mp4"))
        cache.reset_stats()
        sim.find_similar_videos([d], min_match_seconds=10, cache=cache)
        stats3 = cache.stats()
        check("快取:檔案搬移+改名後仍命中", stats3["hits"] == 2 and stats3["misses"] == 0, f"實際={stats3}")

        # 內容改寫後要正確失效
        target = os.path.join(moved, "renamed.mp4")
        with open(target, "r+b") as fh:
            fh.seek(0)
            fh.write(b"\x00" * 4096)
        cache.reset_stats()
        sim.find_similar_videos([d], min_match_seconds=10, cache=cache)
        stats4 = cache.stats()
        check("快取:內容改寫後正確失效重算", stats4["misses"] >= 1, f"實際={stats4}")
    finally:
        cache.close()

    # 配對結論快取(pairs 表):同一批影片重掃第二次,不該再呼叫 _local_align/_estimate_offset
    # ——那是 both_fine/位移投票路徑最貴的部分,配對快取要整段跳過,不是只讓「結果一樣」而已。
    d2 = os.path.join(tmp, "paircache")
    os.makedirs(d2)
    a = content_frames(seed=111, seconds=25)
    b = content_frames(seed=222, seconds=25)
    write_video(os.path.join(d2, "A.mp4"), a)
    write_video(os.path.join(d2, "B.mp4"), a)   # 跟 A 相似(both_fine 路徑)
    write_video(os.path.join(d2, "C.mp4"), b)   # 跟 A/B 都不相似(驗證「不相似」結論也被快取)

    db2 = os.path.join(tmp, "pair_cache_test.db")
    pair_cache = print_cache.PrintCache(db2)
    try:
        first = group_names(sim.find_similar_videos([d2], min_match_seconds=10, cache=pair_cache))
        check("配對快取:首次掃描結果正確", first == [["A.mp4", "B.mp4"]], f"實際={first}")

        orig_align, orig_offset = sim._local_align, sim._estimate_offset
        calls = {"align": 0, "offset": 0}

        def counting_align(*a_, **kw):
            calls["align"] += 1
            return orig_align(*a_, **kw)

        def counting_offset(*a_, **kw):
            calls["offset"] += 1
            return orig_offset(*a_, **kw)

        sim._local_align, sim._estimate_offset = counting_align, counting_offset
        try:
            second = group_names(sim.find_similar_videos([d2], min_match_seconds=10, cache=pair_cache))
        finally:
            sim._local_align, sim._estimate_offset = orig_align, orig_offset

        check("配對快取:重掃結果不變", second == first, f"實際={second}")
        check(
            "配對快取:重掃不再呼叫 DP/位移投票(全部配對快取命中)",
            calls["align"] == 0 and calls["offset"] == 0,
            f"align 呼叫 {calls['align']} 次、offset 呼叫 {calls['offset']} 次(應皆為 0)",
        )
    finally:
        pair_cache.close()

    # DB 損壞不能讓掃描炸掉(快取是純效能優化,壞掉最多是慢)
    with open(db, "wb") as fh:
        fh.write(b"this is definitely not a sqlite database" * 100)
    try:
        broken = print_cache.PrintCache(db)
        try:
            groups = group_names(sim.find_similar_videos([d], min_match_seconds=10, cache=broken))
            check("快取:DB 損壞時自動重建、掃描照常完成", isinstance(groups, list))
        finally:
            broken.close()
    except Exception as e:
        check("快取:DB 損壞時自動重建、掃描照常完成", False, f"丟出例外={e!r}")


# ----------------------------------------------------------------------


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="neonsweep_simtest_")
    print(f"合成影片暫存目錄:{tmp}\n")
    try:
        print("[1] 黑幕誤判防護")
        test_black_frame_guard(tmp)
        print("[2] 資料夾對資料夾模式")
        test_cross_mode(tmp)
        test_cross_overlap_dedup(tmp)
        print("[3] 剪枝不變量")
        test_pruning_invariant()
        print("[4] 重編碼")
        test_reencode(tmp)
        print("[5] 長片涵蓋")
        test_long_video_coverage(tmp)
        print("[6] 精修窗收窄")
        test_refine_window_bounded(tmp)
        print("[7] 取消回應性")
        test_cancel_responsive(tmp)
        print("[8] 階段 1 進度條")
        test_phase1_progress(tmp)
        print("[8b] 完全相同指紋快速通道")
        test_exact_duplicate_fastpath(tmp)
        print("[9] 程序優先權")
        test_background_priority()
        print("[10] 指紋快取")
        test_cache(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _n, ok, _d in _results if ok)
    total = len(_results)
    print(f"\n{'=' * 60}\n{passed}/{total} 通過")
    failed = [n for n, ok, _d in _results if not ok]
    if failed:
        print("失敗:\n  - " + "\n  - ".join(failed))
        return 1
    print("全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
