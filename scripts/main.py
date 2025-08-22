import sys
from PySide6.QtWidgets import QApplication
from flasher_window import FlasherWindow

def main():
    app = QApplication(sys.argv)
    win = FlasherWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()