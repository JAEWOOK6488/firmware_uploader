#!/bin/bash

python3 -m pip install PySide6

sudo apt update
sudo apt install python3-libgpiod -y
sudo apt install -y \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-render-util0 libxcb-xinerama0 libxcb-xinput0 libxkbcommon-x11-0 \
  libegl1-mesa