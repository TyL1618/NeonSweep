import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from cleaner.theme import GLOBAL_QSS
from cleaner.utils.fs import icon_path
from cleaner.views.main_window import MainWindow


def main() -> None:
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
