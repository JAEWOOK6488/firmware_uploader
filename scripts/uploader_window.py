from PySide6.QtWidgets import QWidget, QFileDialog, QMessageBox, QVBoxLayout, QApplication
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
        # 핀 상태는 캐시 기반. None = "아직 모름" (라인을 잡기 전).
        # GUI는 사용자가 명시적으로 버튼을 누르기 전에는 절대 GPIO를 잡지 않는다.
        self._power_hold_pin_state = None
        self._boot0_pin_state = None
        self._request_connected = False
        self._selected_bin_path = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.ui)

        self.setWindowTitle(self.ui.windowTitle() or "Firmware Uploader")
        self.resize(self.ui.size())

        # NOTE: 자동 폴링 타이머는 제거됐다.
        # 이전 코드의 500ms `_gpio_timer`는 power_hold/boot0 라인을 출력 모드로
        # 자동 요청하면서 LOW로 떨어뜨려 캐리어보드 전원을 끊었다. 이제는 사용자가
        # 직접 버튼을 누른 직후에만 라벨을 갱신한다.

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
        # Enter/Exit Update Mode 버튼이 UI에 추가되면 자동 연결.
        if hasattr(u, "enter_update_btn"):
            u.enter_update_btn.clicked.connect(self._on_enter_update_mode)
        if hasattr(u, "exit_update_btn"):
            u.exit_update_btn.clicked.connect(self._on_exit_update_mode)

    @Slot()
    def _on_browse(self):
        # 다이얼로그 열기 전에 큐잉된 이벤트(예: 백그라운드 connect 결과)를 먼저 처리.
        # 그렇지 않으면 사용자가 모르는 사이 dialog event loop 안에서 cmd_done이 처리되어
        # "Connecting..." → "Disconnected" 전이가 dialog 닫힐 때 나타나 마치 dialog가
        # 원인인 것처럼 보인다. 다이얼로그 진입 전에 미리 보이게 한다.
        QApplication.processEvents()

        # 일부 native file dialog는 필터를 case-sensitive로 적용해 *.bin 만으로는
        # .BIN / .Bin 등이 숨겨질 수 있다. 대소문자 변형까지 모두 명시.
        fn, _ = QFileDialog.getOpenFileName(
            self, "프로그램 선택", "",
            "BIN files (*.bin *.BIN *.Bin);;"
            "Binary/Hex (*.bin *.BIN *.hex *.HEX *.elf *.ELF);;"
            "All Files (*)",
            "BIN files (*.bin *.BIN *.Bin)",
            options=QFileDialog.DontUseNativeDialog,
        )
        if not fn:
            return

        if not fn.lower().endswith(".bin"):
            QMessageBox.warning(self, "파일 형식 오류", "BIN(.bin) 파일을 선택해 주세요.")
            return

        self._selected_bin_path = fn
        if hasattr(self.ui, "leFilePath"):
            self.ui.leFilePath.setText(fn)

        self._update_selected_path_label()

    # ---------------- GPIO 버튼 ----------------

    @Slot()
    def _on_set_power_hold_pin(self):
        """
        FW_UPDATE = PMIC EN. HIGH→LOW 전환은 보드 전원을 끊으므로 반드시 confirm.
        첫 클릭이면 안전하게 HIGH로 잡는다(아직 라인을 안 잡았다고 가정).
        """
        cur = gpio.power_hold_cached()  # None이면 아직 라인 미점유

        if cur is None:
            # 아직 출력으로 잡지 않은 상태 → 안전하게 HIGH로 잡기.
            try:
                gpio.power_hold_set(1)
                print("[GPIO] FW_UPDATE first request → HIGH (PMIC EN held)")
            except Exception as e:
                self._show_gpio_error("FW_UPDATE", e)
                return
        elif cur == 0:
            # 이미 LOW 상태 (보드가 살아있다는 건 다른 풀업 / MCU 측에서 잡고 있단 뜻)
            try:
                gpio.power_hold_set(1)
                print("[GPIO] FW_UPDATE → HIGH")
            except Exception as e:
                self._show_gpio_error("FW_UPDATE", e)
                return
        else:
            # HIGH → LOW 전환: 캐리어보드 전원 차단 가능. confirm.
            ans = QMessageBox.warning(
                self, "FW_UPDATE LOW 확인",
                "FW_UPDATE 핀은 PMIC EN과 직결되어 있습니다.\n"
                "LOW로 두면 캐리어보드 전체 전원이 꺼질 수 있습니다.\n\n"
                "정말 LOW로 설정하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
            try:
                gpio.power_hold_set(0)
                print("[GPIO] FW_UPDATE → LOW (사용자 확인됨)")
            except Exception as e:
                self._show_gpio_error("FW_UPDATE", e)
                return

        self._refresh_gpio_label()

    @Slot()
    def _on_set_boot0_pin(self):
        """BOOT_CTRL → STM32 BOOT0. HIGH = system bootloader 진입."""
        cur = gpio.boot0_cached()
        try:
            if cur is None or cur == 0:
                gpio.boot0_set(1)
                print("[GPIO] BOOT_CTRL → HIGH (system bootloader)")
            else:
                gpio.boot0_set(0)
                print("[GPIO] BOOT_CTRL → LOW (normal boot)")
        except Exception as e:
            self._show_gpio_error("BOOT_CTRL", e)
            return
        self._refresh_gpio_label()

    @Slot()
    def _on_set_nrst_pin(self):
        """NRST 펄스(LOW→HIGH). 호출 후 NRST는 HIGH(non-reset)."""
        try:
            gpio.nrst_pulse(low_ms=100)
            print("[GPIO] NRST pulse (LOW 100ms → HIGH)")
        except Exception as e:
            self._show_gpio_error("NRST", e)
            return
        self._refresh_gpio_label()

    # ---------------- Update Mode 시퀀스 ----------------

    @Slot()
    def _on_enter_update_mode(self):
        """
        ROM 부트로더 진입 시퀀스.
        순서가 중요하다:
          1) FW_UPDATE = HIGH  → AP가 PMIC keep-alive 인계 (가장 먼저)
          2) BOOT_CTRL = HIGH  → BOOT0 = system memory bootloader
          3) NRST 펄스 (LOW → HIGH)
        """
        ans = QMessageBox.question(
            self, "Enter Update Mode",
            "다음 시퀀스를 실행합니다:\n"
            "  1) FW_UPDATE = HIGH (PMIC keep-alive 인계)\n"
            "  2) BOOT_CTRL = HIGH (BOOT0 system bootloader)\n"
            "  3) NRST 펄스 (LOW 100ms → HIGH)\n\n"
            "진행하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if ans != QMessageBox.Yes:
            return
        try:
            import time
            gpio.power_hold_set(1); time.sleep(0.05)
            gpio.boot0_set(1);      time.sleep(0.01)
            gpio.nrst_pulse(low_ms=100)
            time.sleep(0.05)
            print("[UpdateMode] Entered")
        except Exception as e:
            self._show_gpio_error("Enter Update Mode", e)
            return
        self._refresh_gpio_label()

    @Slot()
    def _on_exit_update_mode(self):
        """
        앱 펌웨어 부팅 시퀀스.
          1) BOOT_CTRL = LOW
          2) NRST 펄스
          3) (대기 후) FW_UPDATE 라인을 high-Z로 release → MCU/풀업이 인계
        """
        ans = QMessageBox.question(
            self, "Exit Update Mode",
            "다음 시퀀스를 실행합니다:\n"
            "  1) BOOT_CTRL = LOW (정상 부팅)\n"
            "  2) NRST 펄스\n"
            "  3) FW_UPDATE 라인 high-Z release\n\n"
            "MCU가 자체 keep-alive를 잡지 못하면 보드 전원이 꺼질 수 있습니다.\n"
            "진행하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        try:
            import time
            gpio.boot0_set(0);      time.sleep(0.01)
            gpio.nrst_pulse(low_ms=100)
            time.sleep(0.5)
            gpio.fw_update_release()
            print("[UpdateMode] Exited")
        except Exception as e:
            self._show_gpio_error("Exit Update Mode", e)
            return
        self._refresh_gpio_label()

    # ---------------- 라벨/상태 ----------------

    def _show_gpio_error(self, name: str, e: Exception):
        msg = f"{name} GPIO 제어 실패: {e}"
        print(f"[GPIO ERROR] {msg}")
        QMessageBox.critical(self, "GPIO 오류", msg)

    def _set_label_state(self, label, val):
        """val: 0/1/None. None이면 '?' 로 표시."""
        if val is None:
            label.setText("?")
            label.setStyleSheet("color:#6b7280; font-weight:600;")
        elif val == 1:
            label.setText("HIGH")
            label.setStyleSheet("color:#16a34a; font-weight:600;")
        else:
            label.setText("LOW")
            label.setStyleSheet("color:#dc2626; font-weight:600;")

    def _refresh_gpio_label(self):
        """
        sysfs LED brightness를 직접 읽어 라벨을 갱신한다.
        읽기 자체는 라인 상태를 변경하지 않으므로 안전.
        읽기 실패(권한/경로 누락) 시에만 '?'가 표시된다.
        """
        try:
            ph = gpio.power_hold_cached()
            self._power_hold_pin_state = ph
            self._set_label_state(self.ui.power_hold_status_val_label, ph)
            # 토글 버튼 라벨: HIGH 상태면 다음 액션은 OFF로 가는 길이라 표기.
            if ph == 1:
                self.ui.power_hold_btn.setText("OFF")
            else:
                self.ui.power_hold_btn.setText("ON")
        except Exception:
            self.ui.power_hold_status_val_label.setText("ERR")

        try:
            b0 = gpio.boot0_cached()
            self._boot0_pin_state = b0
            self._set_label_state(self.ui.boot0_pin_val_label, b0)
            if b0 == 1:
                self.ui.boot0_btn.setText("OFF")
            else:
                self.ui.boot0_btn.setText("ON")
        except Exception:
            self.ui.boot0_pin_val_label.setText("ERR")

    def _set_comm_status(self, text: str):
        lbl = self.ui.comm_status_val_label
        lbl.setText(text)
        t = text.lower()
        if t.startswith("connected"):
            lbl.setStyleSheet("color:#16a34a; font-weight:600;")    # green
        elif t.startswith("connecting"):
            lbl.setStyleSheet("color:#ca8a04; font-weight:600;")    # amber (in-progress)
        else:
            lbl.setStyleSheet("color:#dc2626; font-weight:600;")    # red

    def _set_flash_status(self, text: str):
        lbl = self.ui.flash_status_val_label
        lbl.setText(text)
        t = text.lower()
        if "fail" in t or "error" in t:
            lbl.setStyleSheet("color:#dc2626; font-weight:600;")        # red
        elif "complete" in t or "done" in t or "ok" in t:
            lbl.setStyleSheet("color:#16a34a; font-weight:600;")        # green
        elif "flash" in t:
            lbl.setStyleSheet("color:#2563eb; font-weight:600;")        # blue (in progress)
        else:
            lbl.setStyleSheet("color:#000000; font-weight:600;")

    # ---------------- Serial ----------------

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
            try:
                self._worker.flash_done.disconnect(self._on_flash_done)
            except Exception:
                pass
            try:
                self._worker.flash_prog.disconnect(self._on_flash_progress)
            except Exception:
                pass
            self._worker.deleteLater()
            self._worker = None

        if not self._serial_thread.isRunning():
            self._serial_thread.start()

        self._worker = SerialWorker(port=port_path, baud=115200, timeout=0.2)
        self._worker.flash_prog.connect(self._on_flash_progress)
        self._worker.flash_done.connect(self._on_flash_done, Qt.QueuedConnection)
        self._worker.moveToThread(self._serial_thread)
        self._worker.cmd_done.connect(self._on_cmd_done, Qt.QueuedConnection)

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
        elif ok and resp:
            # 응답은 왔지만 ACK가 아님 — 정상적인 disconnected와 구분.
            self._set_comm_status(f"No ACK (resp={resp.hex()})")
        else:
            # 무응답: 부트로더 모드가 아니거나 시리얼 미연결.
            self._set_comm_status("No response")

    def _normalize_port(self, dev_text: str) -> str:
        dev_text = (dev_text or "").strip()
        if dev_text.startswith("/dev/"):
            return dev_text
        return f"/dev/{dev_text}"

    def _update_selected_path_label(self):
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

    @Slot(bool, str)
    def _on_flash_done(self, ok: bool, msg: str):
        """워커가 flash_img 종료 시 emit. ok=True면 완료, False면 실패."""
        if ok:
            self._set_flash_status("Flash Complete")
            print("[Flash] Complete")
        else:
            self._set_flash_status(f"Flash Failed: {msg}" if msg else "Flash Failed")
            print(f"[Flash] Failed: {msg}")
            QMessageBox.critical(self, "Flash 실패", msg or "알 수 없는 오류")

    @Slot()
    def _on_flash(self):
        bin_path = self._selected_bin_path or getattr(self.ui, "leFilePath", None).text().strip()
        if not bin_path or not os.path.isfile(bin_path):
            QMessageBox.warning(self, "BIN 파일", "유효한 BIN 파일 경로를 선택하세요.")
            return

        base_addr = 0x08000000
        erase_timeout_s = 20.0
        print(f"[Flash Button] bin={bin_path}, base=0x{base_addr:08X}, erase_to={erase_timeout_s}s")
        self.flash_percent = 0
        self.ui.flash_progress_bar.setValue(0)
        self.ui.flash_progress_bar.setFormat("0%")
        self._set_flash_status("Flashing...")

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
        finally:
            super().closeEvent(event)
