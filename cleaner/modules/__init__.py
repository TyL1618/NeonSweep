from .browser_cache import BrowserCacheModule
from .crash_dumps import CrashDumpsModule
from .dev_caches import DevCachesModule
from .recycle_bin import RecycleBinModule
from .system_temp import SystemTempModule
from .thumbnail_cache import ThumbnailCacheModule
from .user_temp import UserTempModule
from .windows_update import WindowsUpdateModule

# 依序註冊所有模組實例(= UI 顯示順序,見 DEVDOC §4.2)。
ALL_MODULES = [
    UserTempModule(),
    SystemTempModule(),
    BrowserCacheModule(),
    ThumbnailCacheModule(),
    CrashDumpsModule(),
    WindowsUpdateModule(),
    DevCachesModule(),
    RecycleBinModule(),
]
