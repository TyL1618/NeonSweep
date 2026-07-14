# -*- mode: python ; coding: utf-8 -*-
# NeonSweep PyInstaller spec
# 打包指令:pyinstaller NeonSweep.spec
# 輸出位置:dist\NeonSweep.exe(單一執行檔,無主控台)
#
# 注意(DEVDOC §9 / §11 M5):不要設定 uac_admin=True。維持預設 asInvoker,
# 讓程式以一般權限啟動;需要管理員權限的模組由 UI 的「以管理員身分重新啟動」
# 按鈕觸發 ShellExecuteW(..., "runas", ...),而不是每次啟動都跳 UAC。

import os

block_cipher = None

# 第三方工具 smartmontools(GPLv2,https://www.smartmontools.org)的個別檔案,不隨 git
# 版控,需自行下載後放到 third_party/smartmontools/,見 DEVDOC.md §13.1。
#
# 注意:datas 裡故意逐一列出檔名,不是丟整個資料夾路徑(('third_party/smartmontools',
# 'third_party/smartmontools') 這種寫法在實測中被證實不會展開資料夾內容,onefile 解壓
# 後的 _MEIPASS 裡完全沒有這個資料夾——是這裡的 bug,曾誤導成「防毒軟體造成啟動變慢」
# 的錯誤方向,浪費了不少排查時間。改成逐檔列出可以確定 PyInstaller 真的會打包進去)。
# 檔案不存在時該項目直接跳過(讓沒放 smartctl.exe 的開發環境仍能正常打包其他功能),
# 但打包磁碟健康功能前務必確認這三個檔案都存在。
_SMARTCTL_DIR = os.path.join('third_party', 'smartmontools')
_SMARTCTL_FILES = ['smartctl.exe', 'drivedb.h', 'LICENSE.txt']
_smartctl_datas = [
    (os.path.join(_SMARTCTL_DIR, name), _SMARTCTL_DIR)
    for name in _SMARTCTL_FILES
    if os.path.isfile(os.path.join(_SMARTCTL_DIR, name))
]

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('icon.ico', '.'),
        *_smartctl_datas,
    ],
    hiddenimports=[
        'cleaner',
        'cleaner.state',
        'cleaner.theme',
        'cleaner.report',
        'cleaner.workers',
        'cleaner.analysis',
        'cleaner.diagnostics',
        'cleaner.smart_health',
        'cleaner.utils',
        'cleaner.utils.fs',
        'cleaner.utils.admin',
        'cleaner.modules',
        'cleaner.modules.base',
        'cleaner.modules.user_temp',
        'cleaner.modules.system_temp',
        'cleaner.modules.browser_cache',
        'cleaner.modules.thumbnail_cache',
        'cleaner.modules.crash_dumps',
        'cleaner.modules.windows_update',
        'cleaner.modules.dev_caches',
        'cleaner.modules.ai_caches',
        'cleaner.modules.recycle_bin',
        'cleaner.widgets',
        'cleaner.widgets.neon_button',
        'cleaner.widgets.neon_progress',
        'cleaner.widgets.drive_bar',
        'cleaner.widgets.glow_line',
        'cleaner.views',
        'cleaner.views.common',
        'cleaner.views.main_window',
        'cleaner.views.clean_page',
        'cleaner.views.bigfile_page',
        'cleaner.views.dupe_page',
        'cleaner.views.devspace_page',
        'cleaner.views.diagnostic_page',
        'cleaner.views.health_page',
        # send2trash 的 Windows 後端是條件式 import,明確列出避免凍結後漏打包
        'send2trash',
        'send2trash.win',
        'send2trash.win.modern',
        'send2trash.win.legacy',
        'send2trash.win.IFileOperationProgressSink',
        # PyQt6(部分版本需要明確列出)
        'PyQt6.sip',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 不需要的大型套件(僅 scripts/gen_icon.py、scripts/gen_splash.py 這種開發期工具
        # 會用到 PIL,執行期不需要)。注意:tkinter 不能排除——下面的 Splash() 啟動畫面
        # 機制內部依賴 PyInstaller 自帶的 Tcl/Tk 執行期,即使程式本身完全不 import tkinter。
        'matplotlib',
        'numpy',
        'pandas',
        'PIL',
        'scipy',
        'IPython',
        'jupyter',
        'notebook',
        'pytest',
        'setuptools',
        'pkg_resources',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

# 啟動畫面(見 DEVDOC.md §13.1):onefile 模式下,bootloader 解壓縮 + 防毒軟體掃描
# smartctl.exe 可能讓啟動變慢,這張圖會在 Python 都還沒開始跑之前就先顯示,讓使用者
# 知道程式正在啟動、不是當掉。main.py 在主視窗準備顯示前呼叫 pyi_splash.close() 關閉它。
# 圖片來源:python scripts/gen_splash.py(需要時手動重新產生)。
splash = Splash(
    'splash.png',
    binaries=a.binaries,
    datas=a.datas,
    text_pos=(20, 258),
    text_size=11,
    text_color='#8a8a9a',
    text_default='啟動中…',
    always_on_top=True,
)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    a.binaries,
    a.zipfiles,
    a.datas,
    splash.binaries,
    [],
    name='NeonSweep',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,          # 若未安裝 UPX 可改為 False(不影響功能)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # --noconsole:不顯示黑色命令列視窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,    # 維持 asInvoker,不要求 UAC 提升(見上方註解)
    icon='icon.ico',
)
