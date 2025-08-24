from PySide6.QtCore import QObject, Signal, Slot
import serial, struct, time, os

CMD_ACK       = b"\x79"
CMD_NACK      = b"\x1F"
CMD_SYNC      = b"\x7F"
CMD_EXT_ERASE = b"\x44\xBB"
CMD_WRITE     = b"\x31\xCE"

class SerialWorker(QObject):
    cmd_done = Signal(bool, bytes)

    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.2):
        super().__init__()
        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._ser = None  # ★ 지속 연결 핸들

    # ---------- 내부 유틸 ----------
    def _open_port(self) -> bool:
        if self._ser and self._ser.is_open:
            return True
        try:
            self._ser = serial.Serial(
                port=self._port,
                baudrate=self._baud,
                timeout=self._timeout,       # per-read
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_EVEN,   # 8E1
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False, rtscts=False, dsrdtr=False
            )
            try:
                self._ser.setDTR(False); self._ser.setRTS(False)
                self._ser.reset_input_buffer(); self._ser.reset_output_buffer()
            except Exception:
                pass
            time.sleep(0.03)
            return True
        except Exception as e:
            print(f"[serial] open error: {e}")
            self._ser = None
            return False

    def _wait_ack(self, timeout_s: float) -> bool:
        """ACK만 찾고, NACK/노이즈는 무시하며 기다림"""
        if not (self._ser and self._ser.is_open): return False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            b = self._ser.read(1)
            if b == CMD_ACK: return True
            elif b:          continue
        return False

    def _sync_now(self, window_s: float = 5.0) -> bool:
        """window 동안 0x7F 반복 송신하며 ACK 대기"""
        if not (self._ser and self._ser.is_open): return False
        deadline = time.time() + window_s
        while time.time() < deadline:
            self._ser.write(CMD_SYNC); self._ser.flush()
            if self._wait_ack(0.25): return True
            time.sleep(0.03)
        return False

    @Slot()
    def close_port(self):
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        finally:
            self._ser = None

    # ---------- 핑(Handshake): 포트 유지 ----------
    @Slot(bytes, int, float)
    def connect_and_send(self, cmd: bytes, response_size: int = 1, read_timeout_s: float = 1.5):
        try:
            if not self._open_port():
                self.cmd_done.emit(False, b""); return

            resp = bytearray()
            for _ in range(3):
                self._ser.write(cmd); self._ser.flush()
                deadline = time.time() + read_timeout_s
                resp.clear()
                while len(resp) < response_size and time.time() < deadline:
                    chunk = self._ser.read(response_size - len(resp))
                    if chunk: resp.extend(chunk)
                if len(resp) == response_size: break
                time.sleep(0.05)

            self.cmd_done.emit(len(resp) == response_size, bytes(resp))
            # ★ 여기서 포트를 닫지 않습니다 (flash에서 재사용)
        except Exception:
            self.cmd_done.emit(False, b"")

    # ---------- Flash: erase → write (GO 생략) ----------
    @Slot(bytes, int, float)
    def flash_img(self, cmd: bytes, response_size: int = 0x08000000, read_timeout_s: float = 20.0):
        bin_path = cmd.decode("utf-8", errors="ignore").strip()
        base_addr = int(response_size)
        erase_timeout_s = float(read_timeout_s)

        print(f"[flash_img] start: bin='{bin_path}', base=0x{base_addr:08X}, erase_to={erase_timeout_s}s")
        if not os.path.isfile(bin_path):
            print("[flash_img] ERROR: BIN file not found."); return
        fw = open(bin_path, "rb").read()
        total = len(fw)
        if total == 0:
            print("[flash_img] ERROR: BIN is empty."); return
        print(f"[flash_img] BIN size = {total} bytes")

        if not self._open_port():
            print("[flash_img] ERROR: cannot open port"); return

        # 기존 읽기 타임아웃이 너무 짧으면 조금 늘려줌
        old_timeout = self._ser.timeout
        if (old_timeout or 0) < 0.5:
            self._ser.timeout = 0.5

        # 1) 먼저 바로 EXT_ERASE 시도 (세션이 살아있다면 ACK 나올 확률 높음)
        def try_ext_erase() -> bool:
            self._ser.reset_input_buffer()
            self._ser.write(CMD_EXT_ERASE); self._ser.flush()
            if not self._wait_ack(0.8):
                return False
            self._ser.write(b"\xFF\xFF\x00"); self._ser.flush()
            return self._wait_ack(erase_timeout_s)

        if not try_ext_erase():
            print("[flash_img] EXT_ERASE first attempt failed → SYNC then retry")
            # SYNC 재확인
            if not self._sync_now(5.0):
                print("[flash_img] ERROR: Bootloader SYNC failed (no ACK).")
                self._ser.timeout = old_timeout
                return
            if not try_ext_erase():
                print("[flash_img] ERROR: Erase NACK/timeout.")
                self._ser.timeout = old_timeout
                return

        print("[flash_img] Erase OK")

        # 2) Write (256B, 블록당 1회 재시도)
        CHUNK = 256
        written = 0
        addr = base_addr

        def write_block(a: int, data: bytes) -> bool:
            self._ser.write(CMD_WRITE); self._ser.flush()
            if not self._wait_ack(0.8): return False

            addr_bytes = struct.pack(">I", a)
            addr_chk = addr_bytes[0] ^ addr_bytes[1] ^ addr_bytes[2] ^ addr_bytes[3]
            self._ser.write(addr_bytes + bytes([addr_chk])); self._ser.flush()
            if not self._wait_ack(0.8): return False

            lbyte = bytes([len(data) - 1])
            csum = lbyte[0]
            for b in data: csum ^= b
            self._ser.write(lbyte + data + bytes([csum])); self._ser.flush()
            return self._wait_ack(1.5)

        while written < total:
            block = fw[written:written + CHUNK]
            for attempt in range(2):
                if write_block(addr, block):
                    written += len(block); addr += len(block)
                    print(f"[flash_img] Progress {written*100.0/total:5.1f}% ({written}/{total})")
                    break
                else:
                    print(f"[flash_img] WARN: retry @0x{addr:08X} (attempt {attempt+2}/2)")
                    time.sleep(0.05)
            else:
                print(f"[flash_img] ERROR: write block failed @0x{addr:08X}")
                self._ser.timeout = old_timeout
                return

        self._ser.timeout = old_timeout
        print("[flash_img] Write OK (erase+flash complete)")
