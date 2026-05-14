#!/bin/bash

# leds-gpio sysfs brightness는 root만 쓰기 가능 — 실행 시마다 사용자에게
# 쓰기 권한을 부여 (런타임 전용, 재부팅 후 자동 복구).
sudo chmod a+w \
  /sys/class/leds/led_rgb_r/brightness \
  /sys/class/leds/gpio4-c6/brightness \
  /sys/class/leds/gpio0-a0/brightness

# --headless 또는 -H 플래그가 있으면 TUI 모드로 진입.
# 그 외는 기존 GUI 모드.
python3 ../scripts/main.py "$@"
