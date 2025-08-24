# core/control_gpio.py
import gpiod
import threading
from typing import Dict, Tuple

# 핀 정의
POWER_HOLD_GPIO_CHIP = "gpiochip1"
POWER_HOLD_GPIO_LINE = 13

BOOT0_GPIO_CHIP = "gpiochip1"
BOOT0_GPIO_LINE = 14

NRST_GPIO_CHIP = "gpiochip1"
NRST_GPIO_LINE = 15

# 내부 상태
_lock = threading.RLock()
_chips: Dict[str, gpiod.Chip] = {}
_lines: Dict[Tuple[str, int], gpiod.Line] = {}
_line_dir: Dict[Tuple[str, int], str] = {}  # "in" / "out"

def _get_chip(chip_name: str) -> gpiod.Chip:
    with _lock:
        chip = _chips.get(chip_name)
        if chip is None:
            chip = gpiod.Chip(chip_name)
            _chips[chip_name] = chip
        return chip

def _ensure_line(chip_name: str, line_num: int, direction: str) -> gpiod.Line:
    """
    direction: "in" 또는 "out"
    """
    key = (chip_name, line_num)
    with _lock:
        line = _lines.get(key)
        cur_dir = _line_dir.get(key)

        # 라인이 없으면 새로 요청
        if line is None:
            chip = _get_chip(chip_name)
            line = chip.get_line(line_num)
            req_type = gpiod.LINE_REQ_DIR_IN if direction == "in" else gpiod.LINE_REQ_DIR_OUT
            line.request(consumer="fw-uploader", type=req_type)
            _lines[key] = line
            _line_dir[key] = direction
            return line

        # 라인이 있는데 방향이 다르면 재요청
        if cur_dir != direction:
            try:
                line.release()
            except Exception:
                pass
            chip = _get_chip(chip_name)
            line = chip.get_line(line_num)
            req_type = gpiod.LINE_REQ_DIR_IN if direction == "in" else gpiod.LINE_REQ_DIR_OUT
            line.request(consumer="fw-uploader", type=req_type)
            _lines[key] = line
            _line_dir[key] = direction

        return line

def set_gpio(chip_name: str, line_num: int, value: int):
    """지정 라인을 출력으로 요청 후 0/1 설정"""
    line = _ensure_line(chip_name, line_num, "out")
    with _lock:
        line.set_value(1 if value else 0)

def get_gpio_value(chip_name: str, line_num: int, *, as_input: bool = False) -> int:
    """
    현재 값을 읽는다.
    - 기본은 '출력으로 요청된 상태'도 그대로 읽음 (대부분 커널에서 동작)
    - as_input=True면, 안전하게 입력으로 재요청해서 읽고 싶을 때 사용
    """
    direction = "in" if as_input else _line_dir.get((chip_name, line_num), "out")
    line = _ensure_line(chip_name, line_num, direction)
    with _lock:
        return line.get_value()

def cleanup():
    """모든 라인/칩 정리"""
    with _lock:
        for line in _lines.values():
            try:
                line.release()
            except Exception:
                pass
        _lines.clear()
        _line_dir.clear()
        for chip in _chips.values():
            try:
                chip.close()
            except Exception:
                pass
        _chips.clear()


# 편의 함수들 (원하시면 사용)
def power_hold_set(v: int): set_gpio(POWER_HOLD_GPIO_CHIP, POWER_HOLD_GPIO_LINE, v)
def power_hold_get() -> int: return get_gpio_value(POWER_HOLD_GPIO_CHIP, POWER_HOLD_GPIO_LINE)

def boot0_set(v: int): set_gpio(BOOT0_GPIO_CHIP, BOOT0_GPIO_LINE, v)
def boot0_get() -> int: return get_gpio_value(BOOT0_GPIO_CHIP, BOOT0_GPIO_LINE)

def nrst_pulse(low_ms: int = 200):
    """
    NRST 라인을 Low로 잠깐 당겼다가 High로 복귀.
    보드 하드웨어에 따라 active-low일 수 있으니 필요시 반대로 조정.
    """
    import time
    set_gpio(NRST_GPIO_CHIP, NRST_GPIO_LINE, 1)  # active-low reset 가정
    time.sleep(low_ms / 1000.0)
    set_gpio(NRST_GPIO_CHIP, NRST_GPIO_LINE, 0)
