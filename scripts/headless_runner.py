"""
Firmware Uploader - Headless TUI Runner

GUI를 띄우지 않고 4단계로 펌웨어 업데이트를 진행한다:
  1) Bootloader 진입 (FW_UPDATE/BOOT0/NRST 시퀀스)
  2) Connect (시리얼 SYNC + ACK)
  3) BIN 경로 입력
  4) Flash (Erase + Write)

각 단계 시작 전에 "진행하시겠습니까?" 확인을 받는다.
"""

import os
import sys
import time
import struct
import serial

import core.control_gpio as gpio


# ---- STM32 시스템 부트로더 프로토콜 상수 (GUI 코드와 동일) ----
CMD_ACK       = b"\x79"
CMD_NACK      = b"\x1F"
CMD_SYNC      = b"\x7F"
CMD_EXT_ERASE = b"\x44\xBB"
CMD_WRITE     = b"\x31\xCE"

DEFAULT_PORT      = "/dev/ttyS0"
DEFAULT_BAUD      = 115200
DEFAULT_BASE_ADDR = 0x08000000
ERASE_TIMEOUT_S   = 20.0


# ---------------- TUI 유틸 ----------------

def _confirm(prompt: str) -> bool:
    """진행 확인. y/yes만 True. 기본값은 No."""
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _resolve_user_home() -> str:
    """sudo로 실행 시에도 원래 호출 사용자의 home을 반환.
    SUDO_USER가 있으면 그 사용자의 pw_dir을 사용, 아니면 일반 ~ 확장.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            import pwd
            return pwd.getpwnam(sudo_user).pw_dir
        except Exception:
            pass
    return os.path.expanduser("~")


def _expand_path(path: str) -> str:
    """~ 확장을 sudo-aware하게 처리한 후 절대경로로 정규화."""
    if path == "~":
        return _resolve_user_home()
    if path.startswith("~/"):
        return os.path.join(_resolve_user_home(), path[2:])
    # ~user 형태는 평소대로
    return os.path.abspath(os.path.expanduser(path))


def _step(idx: int, total: int, title: str):
    print()
    print(f"━━━ [{idx}/{total}] {title} ━━━")


def _ok(msg: str):
    print(f"  ✓ {msg}")


def _fail(msg: str):
    print(f"  ✗ {msg}")


def _info(msg: str):
    print(f"  • {msg}")


# ---------------- 시리얼 프로토콜 (동기 버전) ----------------

class BootloaderSerial:
    """SerialWorker의 Qt 의존성을 뺀 동기 버전. 같은 프로토콜."""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD, timeout: float = 0.2):
        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._ser = None

    def open(self) -> bool:
        if self._ser and self._ser.is_open:
            return True
        try:
            self._ser = serial.Serial(
                port=self._port,
                baudrate=self._baud,
                timeout=self._timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_EVEN,    # 8E1
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False, rtscts=False, dsrdtr=False,
            )
            try:
                self._ser.setDTR(False); self._ser.setRTS(False)
                self._ser.reset_input_buffer(); self._ser.reset_output_buffer()
            except Exception:
                pass
            time.sleep(0.03)
            return True
        except Exception as e:
            print(f"  [serial] open error: {e}")
            self._ser = None
            return False

    def close(self):
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        finally:
            self._ser = None

    def _wait_ack(self, timeout_s: float) -> bool:
        if not (self._ser and self._ser.is_open):
            return False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            b = self._ser.read(1)
            if b == CMD_ACK:
                return True
            elif b:
                continue
        return False

    def sync(self, window_s: float = 5.0) -> bool:
        """0x7F 반복 송신하며 ACK 대기."""
        if not (self._ser and self._ser.is_open):
            return False
        deadline = time.time() + window_s
        while time.time() < deadline:
            self._ser.write(CMD_SYNC); self._ser.flush()
            if self._wait_ack(0.25):
                return True
            time.sleep(0.03)
        return False

    def flash(self, bin_path: str, base_addr: int = DEFAULT_BASE_ADDR,
              erase_timeout_s: float = ERASE_TIMEOUT_S) -> tuple[bool, str]:
        """Erase + Write. 진행률을 stdout에 한 줄 갱신 형태로 출력."""
        if not os.path.isfile(bin_path):
            return False, "BIN file not found"

        with open(bin_path, "rb") as f:
            fw = f.read()
        total = len(fw)
        if total == 0:
            return False, "BIN is empty"
        _info(f"BIN size = {total:,} bytes")

        if not self.open():
            return False, "cannot open port"

        old_to = self._ser.timeout
        if (old_to or 0) < 0.5:
            self._ser.timeout = 0.5

        # --- Erase ---
        def try_ext_erase() -> bool:
            self._ser.reset_input_buffer()
            self._ser.write(CMD_EXT_ERASE); self._ser.flush()
            if not self._wait_ack(0.8):
                return False
            self._ser.write(b"\xFF\xFF\x00"); self._ser.flush()
            return self._wait_ack(erase_timeout_s)

        _info("Mass erase...")
        if not try_ext_erase():
            _info("Re-SYNC and retry erase")
            if not self.sync(5.0):
                self._ser.timeout = old_to
                return False, "Bootloader SYNC failed"
            if not try_ext_erase():
                self._ser.timeout = old_to
                return False, "Erase NACK/timeout"
        _ok("Erase OK")

        # --- Write ---
        CHUNK = 256
        written = 0
        addr = base_addr

        def write_block(a: int, data: bytes) -> bool:
            self._ser.write(CMD_WRITE); self._ser.flush()
            if not self._wait_ack(0.8):
                return False
            ab = struct.pack(">I", a)
            chk = ab[0] ^ ab[1] ^ ab[2] ^ ab[3]
            self._ser.write(ab + bytes([chk])); self._ser.flush()
            if not self._wait_ack(0.8):
                return False
            lb = bytes([len(data) - 1])
            csum = lb[0]
            for b in data:
                csum ^= b
            self._ser.write(lb + data + bytes([csum])); self._ser.flush()
            return self._wait_ack(1.5)

        last_pct = -1
        while written < total:
            block = fw[written:written + CHUNK]
            for attempt in range(2):
                if write_block(addr, block):
                    written += len(block); addr += len(block)
                    pct = int(written * 100.0 / total)
                    if pct != last_pct:
                        bar_len = 30
                        filled = int(bar_len * pct / 100)
                        bar = "█" * filled + "░" * (bar_len - filled)
                        sys.stdout.write(
                            f"\r  Writing [{bar}] {pct:3d}% ({written:,}/{total:,})"
                        )
                        sys.stdout.flush()
                        last_pct = pct
                    break
                else:
                    time.sleep(0.05)
            else:
                sys.stdout.write("\n")
                self._ser.timeout = old_to
                return False, f"write block failed @0x{addr:08X}"

        sys.stdout.write("\n")
        self._ser.timeout = old_to
        return True, ""


# ---------------- 단계별 함수 ----------------

def step1_enter_bootloader() -> bool:
    _step(1, 5, "Bootloader 진입")
    print("  실행 시퀀스:")
    print("    1) FW_UPDATE = HIGH  (PMIC keep-alive)")
    print("    2) BOOT_CTRL = HIGH  (BOOT0 system bootloader)")
    print("    3) NRST 펄스 (LOW 100ms → HIGH)")
    if not _confirm("  진행하시겠습니까?"):
        _info("취소됨")
        return False
    try:
        gpio.power_hold_set(1); time.sleep(0.05)
        gpio.boot0_set(1);      time.sleep(0.01)
        gpio.nrst_pulse(low_ms=100)
        time.sleep(0.05)
    except Exception as e:
        _fail(f"GPIO 제어 실패: {e}")
        return False
    _ok("Bootloader 진입 시퀀스 완료")
    return True


def step2_connect(port: str) -> BootloaderSerial | None:
    _step(2, 5, "Connect")
    _info(f"포트: {port}, 8E1 @ {DEFAULT_BAUD} bps")
    print("  실행: SYNC(0x7F) 송신 → ACK(0x79) 대기")
    if not _confirm("  진행하시겠습니까?"):
        _info("취소됨")
        return None

    bs = BootloaderSerial(port=port)
    if not bs.open():
        _fail("시리얼 포트 열기 실패")
        return None

    if bs.sync(window_s=5.0):
        _ok(f"Connected (ACK 받음)")
        return bs
    else:
        _fail("SYNC 실패 — 부트로더 모드가 맞는지 확인하세요")
        bs.close()
        return None


def step3_get_bin_path() -> str | None:
    _step(3, 5, "BIN 파일 경로 입력")
    _info("(빈 입력=취소, ~/path 사용 가능)")
    while True:
        try:
            raw = input("  BIN 경로: ").strip()
        except EOFError:
            return None
        if not raw:
            _info("취소됨")
            return None
        path = _expand_path(raw)
        if not os.path.isfile(path):
            _fail(f"파일 없음: {path} — 다시 입력하거나 빈 줄로 취소")
            continue
        if not path.lower().endswith(".bin"):
            _fail(".bin 파일이 아닙니다 — 다시 입력하거나 빈 줄로 취소")
            continue
        sz = os.path.getsize(path)
        _ok(f"파일 확인됨: {path} ({sz:,} bytes)")
        return path


def step4_flash(bs: BootloaderSerial, bin_path: str) -> bool:
    _step(4, 5, "Flash")
    _info(f"Base addr: 0x{DEFAULT_BASE_ADDR:08X}")
    _info(f"Erase timeout: {ERASE_TIMEOUT_S}s")
    _info(f"BIN: {bin_path}")
    if not _confirm("  진행하시겠습니까?"):
        _info("취소됨")
        return False

    ok, msg = bs.flash(bin_path)
    if ok:
        _ok("Flash 완료")
        return True
    else:
        _fail(f"Flash 실패: {msg}")
        return False


def step5_exit_bootloader() -> bool:
    _step(5, 5, "Bootloader 빠져나오기 (앱 펌웨어 부팅)")
    print("  실행 시퀀스:")
    print("    1) BOOT_CTRL = LOW   (정상 부팅)")
    print("    2) NRST 펄스 (LOW 100ms → HIGH)")
    if not _confirm("  진행하시겠습니까?"):
        _info("취소됨 — BOOT0/NRST는 그대로 둡니다")
        return False
    try:
        gpio.boot0_set(0); time.sleep(0.01)
        gpio.nrst_pulse(low_ms=100)
        time.sleep(0.05)
    except Exception as e:
        _fail(f"GPIO 제어 실패: {e}")
        return False
    _ok("Bootloader 종료, 앱 펌웨어로 부팅 시작")
    return True


# ---------------- main ----------------

def main(argv=None):
    print("════════════════════════════════════════")
    print("  Firmware Uploader — Headless Mode")
    print("════════════════════════════════════════")

    # 포트 인자 파싱 (--port /dev/ttyS0)
    port = DEFAULT_PORT
    if argv:
        for i, a in enumerate(argv):
            if a == "--port" and i + 1 < len(argv):
                port = argv[i + 1]

    bs = None
    try:
        if not step1_enter_bootloader():
            return 1
        bs = step2_connect(port)
        if bs is None:
            return 2
        bin_path = step3_get_bin_path()
        if not bin_path:
            return 3
        if not step4_flash(bs, bin_path):
            return 4
        # 시리얼 포트는 5단계 NRST 펄스 전에 닫는 게 안전
        bs.close()
        bs = None
        if not step5_exit_bootloader():
            return 5
        print()
        print("════════════════════════════════════════")
        _ok("모든 단계 완료")
        print("════════════════════════════════════════")
        return 0
    except KeyboardInterrupt:
        print("\n  중단됨 (Ctrl+C)")
        return 130
    finally:
        if bs is not None:
            bs.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
