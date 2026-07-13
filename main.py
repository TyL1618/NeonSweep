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

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
