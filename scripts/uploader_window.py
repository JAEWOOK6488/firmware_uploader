from PySide6.QtWidgets import QWidget, QFileDialog, QMessageBox, QVBoxLayout
from PySide6.QtCore import Slot
from ui_loader import load_ui

class UploaderWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = load_ui("../ui/firmware_uploader.ui")

        # self를 메인 윈도우로 사용하고, ui를 그 안에 붙임
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.ui)

        self.setWindowTitle(self.ui.windowTitle() or "Firmware Uploader")
        self.resize(self.ui.size())  # 초기 크기 맞추기(선택)

        self._wire_signals(self.ui)

    def _wire_signals(self, u):
        u.browser_btn.clicked.connect(self._on_browse)
        u.connect_btn.clicked.connect(self._on_connect)

    @Slot()
    def _on_browse(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "프로그램 선택", "", "Binary/Hex (*.bin *.hex *.elf);;All Files (*)"
        )
        if fn:
            self.ui.leFilePath.setText(fn)

    @Slot()
    def _on_connect(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "프로그램 선택", "", "Binary/Hex (*.bin *.hex *.elf);;All Files (*)"
        )
        if fn:
            self.ui.leFilePath.setText(fn)
