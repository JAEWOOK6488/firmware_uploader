# core/control_gpio.py
#
# 라인 점유 정책: 이 보드의 GPIO4_C5 / GPIO4_C6 / GPIO0_A0 은 부팅 시
# leds-gpio 드라이버가 LED class device로 이미 점유한다 (DT 등록).
# libgpiod로 같은 라인을 다시 잡으려 하면 EBUSY가 난다.
#
# 따라서 여기서는 /sys/class/leds/<name>/brightness 를 통해 제어한다.
# 1 → SoC 패드 HIGH, 0 → LOW (DT에서 active-high로 등록되어 있음).
#
# 핀 매핑 (보드 schematic ↔ DT label ↔ Linux sysfs):
#   FW_UPDATE  GPIO4_C5_3V3 → led_rgb_r → /sys/class/leds/led_rgb_r/brightness
#   BOOT_CTRL  GPIO4_C6_3V3 → gpio4-c6  → /sys/class/leds/gpio4-c6/brightness
#   NRST_CTRL  GPIO0_A0_3V3 → gpio0-a0  → /sys/class/leds/gpio0-a0/brightness
#
# 안전 주의:
#   - FW_UPDATE = LMR14050 PMIC EN. LOW로 떨어지면 캐리어보드 전체
#     전원이 꺼진다. 호출자가 신중하게 다룰 것.
#   - NRST는 SoC ↔ STM32 사이에 인버터가 있어 SoC와 STM32 측 레벨이 반대다.
#       SoC LOW  → STM32 NRST HIGH (정상)
#       SoC HIGH → STM32 NRST LOW  (reset assert)
#     따라서 nrst_pulse는 SoC 기준 LOW→HIGH→LOW 시퀀스.
import os
import time
import threading
from typing import Optional, Dict, Tuple

LED_FW_UPDATE = "/sys/class/leds/led_rgb_r"
LED_BOOT0     = "/sys/class/leds/gpio4-c6"
LED_NRST      = "/sys/class/leds/gpio0-a0"

# 기존 호출부 호환을 위한 chip/line 상수 (실제로는 LED 경로로 매핑됨)
POWER_HOLD_GPIO_CHIP = "gpiochip4"
POWER_HOLD_GPIO_LINE = 21
BOOT0_GPIO_CHIP      = "gpiochip4"
BOOT0_GPIO_LINE      = 22
NRST_GPIO_CHIP       = "gpiochip0"
NRST_GPIO_LINE       = 0

_CHIP_LINE_TO_LED: Dict[Tuple[str, int], str] = {
    (POWER_HOLD_GPIO_CHIP, POWER_HOLD_GPIO_LINE): LED_FW_UPDATE,
    (BOOT0_GPIO_CHIP,      BOOT0_GPIO_LINE):      LED_BOOT0,
    (NRST_GPIO_CHIP,       NRST_GPIO_LINE):       LED_NRST,
}

_lock = threading.RLock()


def _brightness_path(led_dir: str) -> str:
    return os.path.join(led_dir, "brightness")


def _write_brightness(led_dir: str, val: int) -> None:
    if val not in (0, 1):
        raise ValueError(f"brightness must be 0 or 1, got {val}")
    bp = _brightness_path(led_dir)
    # 짧고 직접적인 sysfs write. 권한 없으면 PermissionError.
    with open(bp, "w") as f:
        f.write("1" if val else "0")


def _read_brightness(led_dir: str) -> int:
    bp = _brightness_path(led_dir)
    with open(bp, "r") as f:
        s = f.read().strip()
    return 1 if int(s) > 0 else 0


def _resolve_led(chip_name: str, line_num: int) -> str:
    led = _CHIP_LINE_TO_LED.get((chip_name, line_num))
    if led is None:
        raise RuntimeError(
            f"Unknown gpio mapping: chip={chip_name} line={line_num}"
        )
    return led


def set_gpio(chip_name: str, line_num: int, value: int) -> None:
    """지정 라인의 brightness를 0/1로 설정한다."""
    led = _resolve_led(chip_name, line_num)
    with _lock:
        _write_brightness(led, int(value))


def get_gpio_value(chip_name: str, line_num: int, *, as_input: bool = False) -> int:
    """현재 brightness 값을 읽는다 (= 커널이 출력 중인 라인 레벨)."""
    led = _resolve_led(chip_name, line_num)
    with _lock:
        return _read_brightness(led)


def get_cached_or_none(chip_name: str, line_num: int) -> Optional[int]:
    """
    sysfs 기반에서는 항상 현재 값을 즉시 읽을 수 있다.
    실패(권한/경로 누락) 시에만 None을 반환한다.
    """
    try:
        return get_gpio_value(chip_name, line_num)
    except Exception:
        return None


def cleanup() -> None:
    """sysfs는 영구 자원이 없으므로 할 일 없음."""
    return


# ---------------- 편의 함수 ----------------

def power_hold_set(v: int) -> None:
    """FW_UPDATE = PMIC EN. v=0이면 캐리어보드 전체 전원이 꺼진다."""
    set_gpio(POWER_HOLD_GPIO_CHIP, POWER_HOLD_GPIO_LINE, v)


def power_hold_get() -> int:
    return get_gpio_value(POWER_HOLD_GPIO_CHIP, POWER_HOLD_GPIO_LINE)


def power_hold_cached() -> Optional[int]:
    return get_cached_or_none(POWER_HOLD_GPIO_CHIP, POWER_HOLD_GPIO_LINE)


def boot0_set(v: int) -> None:
    set_gpio(BOOT0_GPIO_CHIP, BOOT0_GPIO_LINE, v)


def boot0_get() -> int:
    return get_gpio_value(BOOT0_GPIO_CHIP, BOOT0_GPIO_LINE)


def boot0_cached() -> Optional[int]:
    return get_cached_or_none(BOOT0_GPIO_CHIP, BOOT0_GPIO_LINE)


def nrst_set(v: int) -> None:
    set_gpio(NRST_GPIO_CHIP, NRST_GPIO_LINE, v)


def nrst_get() -> int:
    return get_gpio_value(NRST_GPIO_CHIP, NRST_GPIO_LINE)


def nrst_cached() -> Optional[int]:
    return get_cached_or_none(NRST_GPIO_CHIP, NRST_GPIO_LINE)


def nrst_pulse(low_ms: int = 100) -> None:
    """
    NRST 펄스. 이 보드는 SoC ↔ STM32 NRST 사이에 인버터가 있어
    SoC 레벨에서는 다음과 같다:
      - 정상(non-reset) 상태: SoC LOW  → STM32 NRST HIGH (deasserted)
      - reset assert 시:    SoC HIGH → STM32 NRST LOW  (asserted)
    따라서 펄스는 LOW(default) → HIGH(low_ms ms) → LOW(default) 순.
    파라미터 이름은 외부 호환을 위해 low_ms 유지 (실제 의미: assert 유지 시간).
    """
    set_gpio(NRST_GPIO_CHIP, NRST_GPIO_LINE, 1)   # assert reset (SoC HIGH = inverter LOW)
    time.sleep(low_ms / 1000.0)
    set_gpio(NRST_GPIO_CHIP, NRST_GPIO_LINE, 0)   # release reset (SoC LOW = inverter HIGH)


def fw_update_release() -> None:
    """
    LED brightness 인터페이스는 라인을 high-Z(입력)로 둘 수 없다 —
    leds-gpio가 출력으로 잡고 있는 한 입력 전환은 불가.
    여기서는 HIGH로 유지하여 PMIC keep-alive를 보장만 한다.
    완전한 high-Z release가 필요하면 leds-gpio unbind + libgpiod 입력 요청이
    필요하며, 호출자 측에서 별도로 수행해야 한다.
    """
    set_gpio(POWER_HOLD_GPIO_CHIP, POWER_HOLD_GPIO_LINE, 1)


def init_safe() -> None:
    """
    안전 디폴트:
      FW_UPDATE = 1 (PMIC keep-alive)
      NRST      = 0 (SoC LOW → 인버터 거쳐 STM32 NRST HIGH = deasserted, 정상 동작)
      BOOT0     = 0 (normal boot)
    """
    set_gpio(POWER_HOLD_GPIO_CHIP, POWER_HOLD_GPIO_LINE, 1)
    time.sleep(0.02)
    set_gpio(NRST_GPIO_CHIP, NRST_GPIO_LINE, 0)
    time.sleep(0.005)
    set_gpio(BOOT0_GPIO_CHIP, BOOT0_GPIO_LINE, 0)
