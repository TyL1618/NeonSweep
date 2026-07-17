"""程序優先權(Windows)。純 ctypes,不碰 Qt,讓純邏輯層也能用。

為什麼需要:相似影片掃描會把所有核心吃滿(cv2 底層的 ffmpeg 解碼預設就會用光核心數,
再乘上 VIDEO_FINGERPRINT_WORKERS 條平行解碼執行緒),使用者實測「掃描時做其他事情都會卡」。

**刻意不用「限制解碼執行緒數」來解**:那會讓掃描本身變慢,而且效果隨機器核心數而異
(4 核跟 16 核的表現完全不同),等於拿確定的損失換不確定的改善。背景掃描本來就**應該**
用光閒置 CPU——問題不在用得多,而在它不讓路。降低優先權剛好只解決後者:前景程式(NORMAL)
永遠優先拿到 CPU,而機器閒著的時候掃描照樣全速跑。

優先權是**程序層級**的屬性,會套用到程序內的所有執行緒——包括 ffmpeg 自己開的解碼執行緒。
這是關鍵:那些執行緒不是我們建的,設定執行緒層級的優先權碰不到它們(Windows 新執行緒一律
從 THREAD_PRIORITY_NORMAL 起跳,不繼承建立者的值)。
"""

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)

NORMAL_PRIORITY_CLASS = 0x00000020
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

# **一定要宣告 restype/argtypes**:ctypes 預設把回傳值當 32-bit int,而 GetCurrentProcess()
# 回傳的 pseudo-handle 是 (HANDLE)-1 = 0xFFFFFFFFFFFFFFFF。預設型別會把它截成 -1 再以 32 位元
# 傳回去,SetPriorityClass 就收到無效 handle、直接失敗(GetLastError=6 ERROR_INVALID_HANDLE)。
# 這個 bug 不會丟例外、只會靜靜地什麼都沒做——實測抓到過一次,別把這幾行拿掉。
_k32 = ctypes.windll.kernel32
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.SetPriorityClass.restype = wintypes.BOOL
_k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
_k32.GetPriorityClass.restype = wintypes.DWORD


def _set_priority_class(value: int) -> bool:
    try:
        return bool(_k32.SetPriorityClass(_k32.GetCurrentProcess(), value))
    except Exception:
        return False


def current_priority_class() -> int:
    """目前的優先權類別(測試用;0 代表查詢失敗)。"""
    try:
        return int(_k32.GetPriorityClass(_k32.GetCurrentProcess()))
    except Exception:
        return 0


class BackgroundPriority:
    """context manager:區間內把整個程序降到 below-normal,離開時還原。

    還原用「離開時無條件設回 NORMAL」而不是「記住進來前的值再設回去」:如果使用者自己用工作
    管理員調過優先權,我們不該假裝知道他要什麼;而且巢狀使用時記錄舊值反而會把 below-normal
    當成「原值」還原回去。這個程式本來就跑在預設的 NORMAL,設回 NORMAL 是對的。

    設定失敗(權限不足之類)只記 log 不丟例外——這是體感優化,不該讓掃描失敗。
    """

    def __enter__(self):
        if _set_priority_class(BELOW_NORMAL_PRIORITY_CLASS):
            logger.info("掃描期間程序優先權已降為 below-normal(前景程式優先,掃描讓路)")
        else:
            logger.warning("無法調整程序優先權,掃描期間可能影響其他程式的反應速度")
        return self

    def __exit__(self, *exc):
        _set_priority_class(NORMAL_PRIORITY_CLASS)
        return False
