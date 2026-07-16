"""驗證用小工具(開發期診斷,不進 NeonSweep.spec 打包清單):測試 cv2 的音訊解碼 API
(CAP_PROP_AUDIO_STREAM 那組屬性)對真實影片檔案是否可靠。

背景:「相似檔案」頁面目前只比對畫面(cleaner/similarity.py),完全忽略音軌。使用者的實際
情境常常是「畫面被大幅修改(浮水印/濾鏡/降畫質)但原始音軌沒動」,音軌會是很強的輔助證據。
但 cv2 這條音訊 API 路徑相對冷門,不同容器/編碼下的可靠度沒有像影像解碼那樣久經考驗,而且
開發機器上沒有 ffmpeg 可以產生測試素材,沒辦法在開發環境驗證——所以請直接拿真實檔案測。

用法:
    python scripts/probe_audio.py "D:\某資料夾\某影片.mp4" ["另一部.mkv" ...]

沒給路徑就跳過測試,只印出用法說明。建議挑幾部「副檔名/來源不同」的影片各測一次
(常見的 mp4、mkv、mov 等),把結果貼回去讓我看,再決定音軌指紋這條路要不要繼續做下去。
"""

import sys
import time


def probe(path: str) -> None:
    import cv2

    print(f"\n=== {path} ===")
    t0 = time.perf_counter()

    # 先試「開檔時直接指定音訊串流」(OpenCV 官方範例的寫法):部分 opencv-python 打包版本的
    # FFmpeg backend 不接受這種 open() 參數組合,會直接開檔失敗——這種情況就退回「先正常開檔、
    # 再用 .set() 切音訊串流」試試看,兩種都失敗才真的判定這個環境沒辦法用這條 API。
    params = [cv2.CAP_PROP_AUDIO_STREAM, 0, cv2.CAP_PROP_VIDEO_STREAM, -1]
    cap = cv2.VideoCapture(path, cv2.CAP_ANY, params)
    opened_via = "開檔時指定參數"
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(path)
        opened_via = "先開檔、後 set()"
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_AUDIO_STREAM, 0)

    if not cap.isOpened():
        print("結果:兩種開檔方式都失敗,這個檔案本身可能有問題(跟音訊 API 支援與否無關)")
        cap.release()
        return

    total_streams = int(cap.get(cv2.CAP_PROP_AUDIO_TOTAL_STREAMS))
    print(f"開檔方式:{opened_via}")
    if total_streams < 0:
        print(f"結果:CAP_PROP_AUDIO_TOTAL_STREAMS 回傳 {total_streams}(負數代表「這個屬性不支援」,")
        print("      不是「這部影片沒有音軌」)")
        print("      => 這個 opencv-python 打包版本的 FFmpeg backend,看起來沒有支援這條音訊 API。")
        print("      => 音軌指紋這個方向如果要做,可能得重新考慮外掛 ffmpeg.exe 或裝 PyAV 之類的額外依賴。")
        cap.release()
        return
    if total_streams == 0:
        print(f"結果:開得起來,音訊 API 有回應,但這個檔案回報 0 個音訊串流(可能真的沒有音軌)")
        cap.release()
        return

    audio_base_index = int(cap.get(cv2.CAP_PROP_AUDIO_BASE_INDEX))
    channels = int(cap.get(cv2.CAP_PROP_AUDIO_TOTAL_CHANNELS))
    sample_rate = cap.get(cv2.CAP_PROP_AUDIO_SAMPLES_PER_SECOND)
    print(f"音訊串流數: {total_streams}  聲道數: {channels}  取樣率: {sample_rate}  base_index: {audio_base_index}")

    samples_read = 0
    frames_grabbed = 0
    max_grabs = 500  # 抓個幾百幀音訊資料就夠判斷可不可行,不用整部讀完
    ok = True
    while frames_grabbed < max_grabs:
        if not cap.grab():
            ok = False
            break
        frames_grabbed += 1
        retrieved, data = cap.retrieve(flag=audio_base_index)
        if retrieved and data is not None:
            samples_read += data.size

    elapsed = time.perf_counter() - t0
    cap.release()

    if samples_read > 0:
        print(f"結果:成功讀到音訊資料——grab 了 {frames_grabbed} 次、累積 {samples_read} 個樣本、耗時 {elapsed:.2f} 秒")
        print("      => 這個檔案的音訊解碼看起來是可行的。")
    else:
        print(f"結果:grab 了 {frames_grabbed} 次(cap.grab() 是否持續成功={ok}),但 retrieve 沒拿到任何樣本")
        print("      => 這個檔案的音訊解碼看起來不可靠,可能需要考慮 ffmpeg.exe / PyAV 這類額外依賴。")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    for path in sys.argv[1:]:
        probe(path)


if __name__ == "__main__":
    main()
