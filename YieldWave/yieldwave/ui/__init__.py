"""UI 包。"""

from . import theme  # noqa: F401  保证 theme 模块在 import ui 时已就绪
from .main_window import MainWindow

__all__ = ["MainWindow", "theme"]
