import ctypes
import os
import sys

from PyQt6.QtWidgets import QApplication


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    if getattr(sys, "frozen", False):        # PyInstaller 打包後
        exe, params = sys.executable, ""
    else:
        exe, params = sys.executable, f'"{os.path.abspath(sys.argv[0])}"'
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if r > 32:                                # 成功才退出目前實例
        QApplication.quit()
