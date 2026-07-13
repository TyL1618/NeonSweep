from .ai_caches import AICachesModule
from .browser_cache import BrowserCacheModule
from .crash_dumps import CrashDumpsModule
from .dev_caches import DevCachesModule
from .recycle_bin import RecycleBinModule
from .system_temp import SystemTempModule
from .thumbnail_cache import ThumbnailCacheModule
from .user_temp import UserTempModule
from .windows_update import WindowsUpdateModule

# 依序註冊所有模組實例(= UI 顯示順序,見 DEVDOC §4.2)。
# ai_caches 是 DEVDOC 原始規格之外、使用者後續要求新增的模組,緊接在 dev_caches 之後
# (概念上同屬「套件/模型下載快取」)。
ALL_MODULES = [
    UserTempModule(),
    SystemTempModule(),
    BrowserCacheModule(),
    ThumbnailCacheModule(),
    CrashDumpsModule(),
    WindowsUpdateModule(),
    DevCachesModule(),
    AICachesModule(),
    RecycleBinModule(),
]
