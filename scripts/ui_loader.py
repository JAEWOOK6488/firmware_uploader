from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import QFile

def load_ui(path: str):
    f = QFile(path)
    if not f.open(QFile.ReadOnly):
        raise RuntimeError(f"UI open failed: {path}")
    try:
        loader = QUiLoader()
        w = loader.load(f)
        if w is None:
            raise RuntimeError("UI load returned None")
        return w
    finally:
        f.close()
