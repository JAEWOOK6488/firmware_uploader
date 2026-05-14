import sys


def _is_headless(argv):
    return any(a in ("--headless", "-H") for a in argv[1:])


def main():
    if _is_headless(sys.argv):
        # GUI(Qt) 의존성을 부르지 않고 헤드리스 러너로 직행
        import headless_runner
        # --headless 플래그는 빼고 나머지 인자만 전달
        rest = [a for a in sys.argv[1:] if a not in ("--headless", "-H")]
        sys.exit(headless_runner.main(rest))

    from PySide6.QtWidgets import QApplication
    from uploader_window import UploaderWindow

    app = QApplication(sys.argv)
    win = UploaderWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
