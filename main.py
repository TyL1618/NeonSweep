import logging
import logging.handlers
import os
import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from cleaner.theme import GLOBAL_QSS
from cleaner.utils.fs import icon_path, user_data_dir
from cleaner.views.main_window import MainWindow


def _setup_logging() -> None:
    """把 log 寫到 %LOCALAPPDATA%\\NeonSweep\\neonsweep.log(輪替,保留兩份)。

    寫檔案而不是只印 stderr,是因為打包後的 exe 是 console=False(見 NeonSweep.spec),
    stderr 根本沒有地方顯示——相似影片掃描的效能儀表(cleaner.similarity 的階段耗時)
    只印 stderr 的話,使用者實測完根本拿不到數字。開發模式下額外印一份到 stderr 方便即時看。

    設定失敗(例如目錄不可寫)不能讓 App 起不來:logging 純屬診斷,吞掉例外照常啟動。
    """
    try:
        handlers: list[logging.Handler] = [
            logging.handlers.RotatingFileHandler(
                os.path.join(user_data_dir(), "neonsweep.log"),
                maxBytes=2 * 1024 * 1024,
                backupCount=1,
                encoding="utf-8",
            )
        ]
        if not getattr(sys, "frozen", False):
            handlers.append(logging.StreamHandler())
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=handlers,
        )
    except Exception:
        pass


def main() -> None:
    _setup_logging()
    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv)
    app.setStyleSheet(GLOBAL_QSS)
    app.setWindowIcon(QIcon(icon_path()))

    window = MainWindow()
    window.show()

    # 關掉 PyInstaller onefile 打包的啟動畫面(見 NeonSweep.spec 的 Splash())。
    # pyi_splash 只在有搭配 Splash() 打包的 frozen exe 裡才存在,開發模式下 import 會
    # 失敗,屬預期行為。
    try:
        import pyi_splash

        pyi_splash.close()
    except ImportError:
        pass

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
