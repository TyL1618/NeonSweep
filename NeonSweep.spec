# -*- mode: python ; coding: utf-8 -*-
# NeonSweep PyInstaller spec
# 打包指令:pyinstaller NeonSweep.spec
# 輸出位置:dist\NeonSweep.exe(單一執行檔,無主控台)
#
# 注意(DEVDOC §9 / §11 M5):不要設定 uac_admin=True。維持預設 asInvoker,
# 讓程式以一般權限啟動;需要管理員權限的模組由 UI 的「以管理員身分重新啟動」
# 按鈕觸發 ShellExecuteW(..., "runas", ...),而不是每次啟動都跳 UAC。

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('icon.ico', '.'),
    ],
    hiddenimports=[
        'cleaner',
        'cleaner.state',
        'cleaner.theme',
        'cleaner.report',
        'cleaner.workers',
        'cleaner.analysis',
        'cleaner.diagnostics',
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
        # 不需要的大型套件(僅 scripts/gen_icon.py 這種開發期工具會用到 PIL,執行期不需要)
        'tkinter',
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
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
