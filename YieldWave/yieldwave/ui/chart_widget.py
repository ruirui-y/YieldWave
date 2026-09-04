"""历史走势图：PyQt6 + matplotlib。

绘制最近 1/2/3/6/12 个月的 dividend_yield_2 曲线，叠加 M42 与 A/B/C 买卖线。
若本周已锁定策略，买卖线保持水平（使用锁定值）；否则使用实时 M42 计算的水平线。
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional

import matplotlib

matplotlib.use("QtAgg")

# 中文显示：优先使用系统中文字体，避免图表中文变方块
from matplotlib import font_manager

_CJK_CANDIDATES = ["Microsoft YaHei", "SimHei", "PingFang SC", "Heiti SC", "Noto Sans CJK SC"]
_available_fonts = {f.name for f in font_manager.fontManager.ttflist}
_for_cjk = next((c for c in _CJK_CANDIDATES if c in _available_fonts), None)
if _for_cjk:
    matplotlib.rcParams["font.sans-serif"] = [_for_cjk, "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import ValuationRecord
from ..strategy import compute_thresholds, rolling_median

_RANGES = {
    "1个月": 21,
    "2个月": 42,
    "3个月": 63,
    "6个月": 126,
    "1年": 252,
}


class ChartWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._records: List[ValuationRecord] = []
        self._weekly_m42: Optional[float] = None
        self._thresholds: Dict[str, float] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("时间范围："))
        self.range_combo = QComboBox()
        self.range_combo.addItems(list(_RANGES.keys()))
        self.range_combo.setCurrentText("6个月")
        self.range_combo.currentTextChanged.connect(self.redraw)
        top.addWidget(self.range_combo)
        top.addStretch(1)
        layout.addLayout(top)

        self.figure = Figure(figsize=(8, 4.2), dpi=100)
        self.canvas = FigureCanvas(self.figure)
        layout.addWidget(self.canvas)

    def set_data(
        self,
        records: List[ValuationRecord],
        thresholds: Dict[str, float],
        weekly_m42: Optional[float] = None,
    ) -> None:
        self._records = records
        self._thresholds = thresholds
        self._weekly_m42 = weekly_m42
        self.redraw()

    def redraw(self) -> None:
        if not self._records:
            return
        days = _RANGES[self.range_combo.currentText()]
        recs = self._records[-days:] if days < len(self._records) else self._records
        dates = [r.date for r in recs]
        dy2 = [r.dividend_yield_2 for r in recs]

        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.plot(dates, dy2, label="股息率2 (D/P2)", color="#1f77b4", linewidth=1.6)
        # M42 滚动中位数
        vals = [r.dividend_yield_2 for r in self._records if r.dividend_yield_2 is not None]
        m42_series = []
        for i in range(len(self._records)):
            if self._records[i].dividend_yield_2 is None:
                m42_series.append(None)
                continue
            m42_series.append(rolling_median(vals[: i + 1], 42))
        m42_visible = m42_series[-len(recs):]
        ax.plot(dates, m42_visible, label="M42 (42日中位数)", color="#555555", linestyle="--", linewidth=1.2)

        # 买卖线（水平）
        colors = {
            "A_buy": "#2ca02c", "A_sell": "#d62728",
            "B_buy": "#9467bd", "B_sell": "#8c564b",
            "C_buy": "#17becf", "C_sell": "#e377c2",
        }
        for key, col in colors.items():
            if key in self._thresholds:
                ax.axhline(self._thresholds[key], color=col, linestyle=":", linewidth=1.0,
                           label=key.replace("_", " "))

        ax.set_title("中证红利低波 H30269 · 股息率2 历史走势")
        ax.set_ylabel("股息率 (%)")
        ax.legend(fontsize=7, loc="upper right")
        ax.grid(True, alpha=0.3)
        try:
            self.figure.autofmt_xdate()
        except Exception:
            pass
        self.canvas.draw()
