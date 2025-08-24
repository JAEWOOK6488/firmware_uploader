from PySide6.QtWidgets import QWidget, QFileDialog, QMessageBox, QVBoxLayout
from PySide6.QtCore import Slot, QTimer, QThread, Qt, Signal
from ui_loader import load_ui
from core.serial_communication import SerialWorker
import core.control_gpio as gpio
import os

CMD_ACK       = b"\x79"
CMD_NACK      = b"\x1F"
CMD_SYNC      = b"\x7F"
CMD_EXT_ERASE = b"\x44\xBB"
CMD_WRITE     = b"\x31\xCE"
BOOT_SYNC     = b"\x7F"

class UploaderWindow(QWidget):
    # 워커 슬롯 시그니처와 동일하게 정의
    request_cmd = Signal(bytes, int, float)
    request_flash_img = Signal(bytes, int, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = load_ui("../ui/firmware_uploader.ui")

        self.flash_percent = 0
        self._power_hold_pin_state = 0
        self._boot0_pin_state = 0
        self._nrst_pin_state = 0
        self._request_connected = False  # request_cmd 연결 상태 플래그
        self._selected_bin_path = ""

        # self를 메인 윈도우로 사용하고, ui를 그 안에 붙임
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.ui)

        self.setWindowTitle(self.ui.windowTitle() or "Firmware Uploader")
        self.resize(self.ui.size())

        # GPIO 상태 폴링
        self._gpio_timer = QTimer(self)
        self._gpio_timer.timeout.connect(self._refresh_gpio_label)
        self._gpio_timer.start(500)  # 500ms

        self._wire_signals(self.ui)
        self._refresh_gpio_label()

        # Serial
        self._serial_thread = QThread(self)
        self._worker = None

        self._set_comm_status("Disconnected")

    def _wire_signals(self, u):
        u.browser_btn.clicked.connect(self._on_browse)
        u.connect_btn.clicked.connect(self._on_connect)
        u.power_hold_btn.clicked.connect(self._on_set_power_hold_pin)
        u.boot0_btn.clicked.connect(self._on_set_boot0_pin)
        u.nrst_btn.clicked.connect(self._on_set_nrst_pin)
        if hasattr(u, "flash_btn"):
            u.flash_btn.clicked.connect(self._on_flash)

    @Slot()
    def _on_browse(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "프로그램 선택", "",
            "BIN files (*.bin);;Binary/Hex (*.bin *.hex *.elf);;All Files (*)"
        )
        if not fn:
            return

        # .bin만 허용하고 싶다면 아래 체크 유지 (원치 않으면 if 블록 삭제)
        if not fn.lower().endswith(".bin"):
            QMessageBox.warning(self, "파일 형식 오류", "BIN(.bin) 파일을 선택해 주세요.")
            return

        self._selected_bin_path = fn
        # 기존 라인에디트도 계속 쓰고 있다면 갱신
        if hasattr(self.ui, "leFilePath"):
            self.ui.leFilePath.setText(fn)

        # 라벨 갱신
        self._update_selected_path_label()

    @Slot()
    def _on_set_power_hold_pin(self):
        if self._power_hold_pin_state == 0:
            gpio.power_hold_set(1)
            print("Set HIGH.")
        else:
            gpio.power_hold_set(0)
            print("Set LOW.")

    @Slot()
    def _on_set_boot0_pin(self):
        if self._boot0_pin_state == 0:
            gpio.boot0_set(1)
            print("Set HIGH.")
        else:
            gpio.boot0_set(0)
            print("Set LOW.")

    @Slot()
    def _on_set_nrst_pin(self):
        gpio.nrst_pulse()

    @Slot()
    def _refresh_gpio_label(self):
        try:
            power_hold_val = gpio.power_hold_get()  # 0 or 1
            self._power_hold_pin_state = power_hold_val
            power_hold_label = self.ui.power_hold_status_val_label
            if power_hold_val == 1:
                power_hold_label.setText("HIGH")
                power_hold_label.setStyleSheet("color:#16a34a; font-weight:600;")
                self.ui.power_hold_btn.setText("OFF")
            else:
                power_hold_label.setText("LOW")
                power_hold_label.setStyleSheet("color:#dc2626; font-weight:600;")
                self.ui.power_hold_btn.setText("ON")

        except Exception:
            # 권한/라인번호 오류 등
            self.ui.power_hold_status_val_label.setText("ERR")

        try:
            boot0_val = gpio.boot0_get()  # 0 or 1
            self._boot0_pin_state = boot0_val
            boot_0_label = self.ui.boot0_pin_val_label
            if boot0_val == 1:
                boot_0_label.setText("HIGH")
                boot_0_label.setStyleSheet("color:#16a34a; font-weight:600;")
                self.ui.boot0_btn.setText("OFF")
            else:
                boot_0_label.setText("LOW")
                boot_0_label.setStyleSheet("color:#dc2626; font-weight:600;")
                self.ui.boot0_btn.setText("ON")
        except Exception:
            self.ui.boot0_pin_val_label.setText("ERR")

        if self.flash_percent == 100:
            self._set_flash_status("Flash Done")

    def _set_comm_status(self, text: str):
        lbl = self.ui.comm_status_val_label
        lbl.setText(text)
        if text.lower().startswith("conn"):
            lbl.setStyleSheet("color:#16a34a; font-weight:600;")
        else:
            lbl.setStyleSheet("color:#dc2626; font-weight:600;")

    def _set_flash_status(self, text: str):
        lbl = self.ui.flash_status_val_label
        lbl.setText(text)
        if text.lower().startswith("fla"):
            lbl.setStyleSheet("color:#16a34a; font-weight:600;") 
        else:
            lbl.setStyleSheet("color:#000000; font-weight:600;") 


    @Slot()
    def _on_connect(self):
        dev_text = self.ui.device_name_le.text()
        port_path = self._normalize_port(dev_text)
        print(f"[Connect Button] Trying to open device: {port_path}")
        self._set_comm_status("Connecting...")

        # 이전 연결 해제
        if self._request_connected and self._worker is not None:
            try:
                self.request_cmd.disconnect(self._worker.connect_and_send)
                self.request_flash_img.disconnect(self._worker.flash_img)
            except Exception:
                pass
            self._request_connected = False

        if self._worker is not None:
            try:
                self._worker.cmd_done.disconnect(self._on_cmd_done)
            except Exception:
                pass
            self._worker.deleteLater()
            self._worker = None

        if not self._serial_thread.isRunning():
            self._serial_thread.start()

        self._worker = SerialWorker(port=port_path, baud=115200, timeout=0.2)
        self._worker.flash_prog.connect(self._on_flash_progress)
        self._worker.moveToThread(self._serial_thread)
        self._worker.cmd_done.connect(self._on_cmd_done, Qt.QueuedConnection)

        # 시그널 연결
        self.request_cmd.connect(self._worker.connect_and_send, Qt.QueuedConnection)
        self.request_flash_img.connect(self._worker.flash_img, Qt.QueuedConnection)
        self._request_connected = True

        # 부트로더 ACK 테스트
        self.request_cmd.emit(BOOT_SYNC, 1, 2.0)

    @Slot(bool, bytes)
    def _on_cmd_done(self, ok: bool, resp: bytes):
        print(f"[Serial Response] ok={ok}, resp={resp.hex() if resp else 'None'}")
        
        if ok and resp == CMD_ACK:
            self._set_comm_status("Connected")
        else:
            self._set_comm_status("Disconnected")

    def _normalize_port(self, dev_text: str) -> str:
        dev_text = (dev_text or "").strip()
        if dev_text.startswith("/dev/"):
            return dev_text
        return f"/dev/{dev_text}"

    def _update_selected_path_label(self):
        """selected_file_path_label에 경로를 예쁘게(가운데 생략) 표시하고, 툴팁에는 전체 경로 표시"""
        if not hasattr(self.ui, "selected_file_path_label"):
            return
        label = self.ui.selected_file_path_label
        text = self._selected_bin_path or "-"
        elided = label.fontMetrics().elidedText(text, Qt.ElideMiddle, label.width())
        label.setText(elided)
        label.setToolTip(text)

    def _on_flash_progress(self, percent: int):
        self.ui.flash_progress_bar.setRange(0, 100)
        self.ui.flash_progress_bar.setValue(percent)
        self.ui.flash_progress_bar.setFormat(f"{percent}%")
        self.ui.flash_progress_bar.setTextVisible(True)
        self.flash_percent = percent

    @Slot()
    def _on_flash(self):
        # 선택 경로 확인: label/lineedit 중 하나에서 가져오기
        bin_path = self._selected_bin_path or getattr(self.ui, "leFilePath", None).text().strip()
        if not bin_path or not os.path.isfile(bin_path):
            QMessageBox.warning(self, "BIN 파일", "유효한 BIN 파일 경로를 선택하세요.")
            return

        base_addr = 0x08000000
        erase_timeout_s = 20.0
        print(f"[Flash Button] bin={bin_path}, base=0x{base_addr:08X}, erase_to={erase_timeout_s}s")
        self._set_flash_status("Flashing...")

        # flash_img(bytes,int,float) 호출
        self.request_flash_img.emit(bin_path.encode("utf-8"), base_addr, erase_timeout_s)

    def closeEvent(self, event):
        try:
            if self._request_connected and self._worker is not None:
                try:
                    self.request_cmd.disconnect(self._worker.connect_and_send)
                except Exception:
                    pass
                self._request_connected = False

            if self._worker:
                try:
                    self._worker.cmd_done.disconnect(self._on_cmd_done)
                except Exception:
                    pass
                self._worker.deleteLater()
                self._worker = None
            
            if self._worker and hasattr(self._worker, "close_port"):
                self._worker.close_port()

            if self._serial_thread.isRunning():
                self._serial_thread.quit()
                self._serial_thread.wait(500)

            if self._gpio_timer.isActive():
                self._gpio_timer.stop()
        finally:
            super().closeEvent(event)
