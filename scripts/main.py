import sys
from PySide6.QtWidgets import QApplication
from uploader_window import UploaderWindow

def main():
    app = QApplication(sys.argv)
    win = UploaderWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()