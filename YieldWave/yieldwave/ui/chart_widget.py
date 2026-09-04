"""历史走势图：PyQt6 + matplotlib（深色主题 + Hover 交互 + 周锁定阶梯线）。

绘制最近 1/2/3/6/12 个月的 D/P2 曲线、滚动 M42、按周锁定的 A/B/C 买卖阶梯线。

Hover 交互（按文档要求）：
- 鼠标进入 axes 后，按 x 坐标用 numpy.searchsorted 吸附到最近交易日；
- 显示竖向虚线 crosshair + 该日 D/P2 marker + tooltip annotation；
- 鼠标离开 axes：隐藏 crosshair/marker/tooltip。

性能（按文档要求）：
- 鼠标移动只改 set_xdata / set_data / xy / text，然后 canvas.draw_idle()；
- 禁止在 mouse move 时 ax.clear() 或重新查 SQLite / 重新计算整段历史。
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Sequence

import matplotlib

matplotlib.use("QtAgg")

# 中文显示：优先使用系统中文字体
from matplotlib import font_manager

_CJK_CANDIDATES = ["Microsoft YaHei", "SimHei", "PingFang SC", "Heiti SC", "Noto Sans CJK SC"]
_available_fonts = {f.name for f in font_manager.fontManager.ttflist}
_for_cjk = next((c for c in _CJK_CANDIDATES if c in _available_fonts), None)
if _for_cjk:
    matplotlib.rcParams["font.sans-serif"] = [_for_cjk, "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
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
from ..precision import D, fmt_yield
from ..strategy import (
    compute_thresholds,
    rolling_median,
    weekly_locked_thresholds_for_records,
)
from . import theme

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
        # 数据
        self._records: List[ValuationRecord] = []
        self._weekly_m42: Optional[Decimal] = None  # 当前周锁定 M42（仅用于"今天起一条水平线"）
        self._thresholds: Dict[str, Decimal] = {}
        # 当前可见窗口的缓存（redraw 时刷新，hover 直接用，不重新计算）
        self._visible_dates: List[_dt.date] = []
        self._visible_dp2: List[float] = []
        self._visible_m42: List[float] = []
        self._visible_idx: np.ndarray = np.array([], dtype=float)
        self._visible_locks: List[Optional[Dict[str, Decimal]]] = []
        self._build_ui()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        rng_label = QLabel("时间范围：")
        rng_label.setObjectName("KpiLabel")
        top.addWidget(rng_label)
        self.range_combo = QComboBox()
        self.range_combo.addItems(list(_RANGES.keys()))
        self.range_combo.setCurrentText("6个月")
        self.range_combo.currentTextChanged.connect(self.redraw)
        top.addWidget(self.range_combo)
        top.addStretch(1)
        self._hint_label = QLabel(
            "鼠标移入走势图自动吸附最近交易日 · 显示 D/P2 与周锁定阈值"
        )
        self._hint_label.setObjectName("MutedNote")
        top.addWidget(self._hint_label)
        layout.addLayout(top)

        self.figure = Figure(figsize=(8, 4.2), dpi=100, facecolor=theme.APP_BG)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setStyleSheet(f"background-color: {theme.APP_BG}; border: none;")
        layout.addWidget(self.canvas)

        # 初始 axes + hover 元素
        self._ax = self.figure.add_subplot(111)
        self._apply_dark_axes(self._ax)
        # 隐藏 crosshair / marker / annotation（redraw 时创建/重置）
        self._crosshair_v: Optional[Line2D] = None
        self._hover_marker: Optional[Line2D] = None
        self._annotation = None
        # 绑定鼠标事件
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("axes_leave_event", self._on_axes_leave)

    @staticmethod
    def _apply_dark_axes(ax) -> None:
        """统一深色 axes 样式（figure/axes/spines/ticks/grid/legend）。"""
        ax.set_facecolor(theme.CARD_BG)
        for spine in ax.spines.values():
            spine.set_color(theme.BORDER)
        ax.tick_params(colors=theme.TEXT_SECONDARY, which="both")
        for label in (ax.get_xticklabels() + ax.get_yticklabels()):
            label.set_color(theme.TEXT_SECONDARY)
        ax.xaxis.label.set_color(theme.TEXT_PRIMARY)
        ax.yaxis.label.set_color(theme.TEXT_PRIMARY)
        ax.title.set_color(theme.TEXT_PRIMARY)
        ax.grid(True, color=theme.BORDER, alpha=0.5, linestyle="-", linewidth=0.6)

    # ---------------- 数据接口 ----------------
    def set_data(
        self,
        records: List[ValuationRecord],
        thresholds: Dict[str, object],
        weekly_m42: Optional[float] = None,
    ) -> None:
        self._records = records
        self._thresholds = {k: D(v) for k, v in thresholds.items() if v is not None}
        self._weekly_m42 = D(weekly_m42)
        self.redraw()

    def set_range(self, text: str) -> None:
        """由主窗口自建下拉框驱动；设置内部 combo 文本并主动重绘。

        主动 redraw 而不只依赖 currentTextChanged 信号：Qt 在文本未变时
        不会发射信号，若首选项与当前一致会导致图不刷新（单一数据源兜底）。
        """
        if text in _RANGES:
            self.range_combo.setCurrentText(text)
            self.redraw()

    def redraw(self) -> None:
        """重画整张图（初始化/切换时间范围时调用；hover 不调用此函数）。"""
        if not self._records:
            return
        days = _RANGES[self.range_combo.currentText()]
        recs = self._records[-days:] if days < len(self._records) else self._records
        dates = [r.date for r in recs]
        dy2: List[float] = [
            float(r.dividend_yield_2) if r.dividend_yield_2 is not None else float("nan")
            for r in recs
        ]

        # 滚动 M42：用全部历史算，取可见区段
        full_dy2 = [r.dividend_yield_2 for r in self._records if r.dividend_yield_2 is not None]
        m42_full: List[float] = []
        for i in range(len(self._records)):
            if self._records[i].dividend_yield_2 is None:
                m42_full.append(float("nan"))
                continue
            m = rolling_median(full_dy2[: i + 1], 42)
            m42_full.append(float(m) if m is not None else float("nan"))
        m42_visible = m42_full[-len(recs):]

        # 周锁定阶梯线（每条记录一个锁定阈值 dict 或 None）
        # 用周锁定函数一次性算出整段历史（无未来泄漏）
        all_locks = weekly_locked_thresholds_for_records(
            self._records, _build_thresholds_config(), 42
        )
        visible_locks = all_locks[-len(recs):]

        # 缓存到实例（hover 直接用，不再重算）
        self._visible_dates = dates
        self._visible_dp2 = dy2
        self._visible_m42 = m42_visible
        self._visible_idx = np.arange(len(dates), dtype=float)
        self._visible_locks = visible_locks

        # ---- 清空 axes 并重画 ----
        self._ax.clear()
        self._apply_dark_axes(self._ax)
        # D/P2 主线
        self._ax.plot(
            self._visible_idx, dy2,
            label="股息率2 (D/P2)", color=theme.DP2_LINE, linewidth=1.6, marker="",
        )
        # M42 滚动虚线
        self._ax.plot(
            self._visible_idx, m42_visible,
            label="M42 (42日中位数)", color=theme.M42_LINE, linestyle="--", linewidth=1.1,
        )

        # ---- A/B/C 周锁定阶梯线（关键：阶梯而非直线横贯） ----
        self._draw_locked_step_lines(visible_locks)

        # ---- 当前周额外标注：本周锁定的 M42 水平线（仅作"今日参考"，红色虚线，仅最新一日） ----
        if self._weekly_m42 is not None:
            self._ax.axhline(
                float(self._weekly_m42), color=theme.WARNING,
                linestyle=":", linewidth=0.9, alpha=0.6,
            )
            self._ax.text(
                self._visible_idx[-1], float(self._weekly_m42),
                f" 本周锁定 M42={fmt_yield(self._weekly_m42, 3)}",
                color=theme.WARNING, fontsize=8, ha="right", va="bottom",
            )

        # ---- 轴标签 / 标题 / 图例 ----
        self._ax.set_title("中证红利低波 H30269 · 股息率2 历史走势", color=theme.TEXT_PRIMARY)
        self._ax.set_ylabel("股息率 (%)", color=theme.TEXT_PRIMARY)
        self._ax.set_xlabel("交易日序号（按时间从左到右）", color=theme.TEXT_PRIMARY)
        # x 轴显示日期刻度（每 ~N 个一个 label）
        self._set_date_ticks(dates)
        leg = self._ax.legend(fontsize=8, loc="upper right", framealpha=0.6, facecolor=theme.CARD_BG_ALT)
        if leg:
            for txt in leg.get_texts():
                txt.set_color(theme.TEXT_SECONDARY)

        # ---- 创建 hover 元素（一次性，hover 只 set_data） ----
        self._crosshair_v = self._ax.axvline(
            self._visible_idx[0], color=theme.TEXT_SECONDARY,
            linestyle="--", linewidth=0.8, alpha=0.0, visible=False,
        )
        self._hover_marker = Line2D(
            [self._visible_idx[0]], [dy2[0] if dy2 else 0],
            marker="o", linestyle="", color=theme.WARNING,
            markersize=8, markeredgecolor=theme.APP_BG, markeredgewidth=1.5,
            alpha=0.0, visible=False, zorder=10,
        )
        self._ax.add_line(self._hover_marker)
        self._annotation = self._ax.annotate(
            "", xy=(0, 0), xytext=(20, 20), textcoords="offset points",
            bbox=dict(
                boxstyle="round,pad=0.5",
                facecolor=theme.CARD_BG_ALT, edgecolor=theme.BORDER, linewidth=1.0,
            ),
            font="Microsoft YaHei" if _for_cjk else "DejaVu Sans",
            color=theme.TEXT_PRIMARY, fontsize=9,
            arrowprops=dict(arrowstyle="-", color=theme.TEXT_MUTED, lw=0.5),
            annotation_clip=False, alpha=0.0, visible=False,
        )

        self.canvas.draw_idle()

    def _draw_locked_step_lines(self, locks: Sequence[Optional[Dict[str, Decimal]]]) -> None:
        """绘制 A/B/C 周锁定阶梯线。

        每个交易日的阈值 = 当周锁定值（周一锁定，整周不变），所以画出来是阶梯状。
        每条 key（A_buy/A_sell/...）一条独立阶梯线，颜色由 THRESHOLD_COLORS 统一管理。
        """
        if not locks:
            return
        keys = ["A_buy", "A_sell", "B_buy", "B_sell", "C_buy", "C_sell"]
        # 把每条 key 的"按日"序列收集出来
        series: Dict[str, List[Optional[float]]] = {k: [] for k in keys}
        for daily in locks:
            for k in keys:
                v = None
                if daily and k in daily and daily[k] is not None:
                    v = float(daily[k])
                series[k].append(v)

        x = self._visible_idx
        for k in keys:
            ys = series[k]
            if not any(y is not None for y in ys):
                continue
            # step where：后侧阶梯，使周内保持水平，到下一周第一个交易日跳变
            # 用前向填充，把 None 替换为前一个非空值（无前值则用 0 占位但不画）
            ys_filled: List[float] = []
            last = None
            for y in ys:
                if y is None:
                    ys_filled.append(last if last is not None else float("nan"))
                else:
                    ys_filled.append(y)
                    last = y
            color = theme.THRESHOLD_COLORS.get(k, theme.TEXT_MUTED)
            self._ax.step(
                x, ys_filled, where="post",
                color=color, linewidth=1.0, linestyle="-",
                alpha=0.85, label=k.replace("_", " "),
            )

    def _set_date_ticks(self, dates: List[_dt.date]) -> None:
        """x 轴刻度：均匀取 ~6 个日期，显示 MM-DD。"""
        n = len(dates)
        if n == 0:
            return
        step = max(1, n // 6)
        ticks_pos = list(range(0, n, step))
        if ticks_pos[-1] != n - 1:
            ticks_pos.append(n - 1)
        labels = [dates[i].strftime("%m-%d") for i in ticks_pos]
        self._ax.set_xticks(ticks_pos)
        self._ax.set_xticklabels(labels, color=theme.TEXT_SECONDARY, fontsize=8, rotation=30, ha="right")

    # ---------------- Hover 事件 ----------------
    def _on_motion(self, event) -> None:
        """鼠标移动：吸附最近交易日 + 更新 crosshair/marker/tooltip。"""
        if event.inaxes != self._ax or not self._visible_idx.size:
            return
        if self._crosshair_v is None or self._hover_marker is None or self._annotation is None:
            return
        x = event.xdata
        if x is None:
            return
        # numpy.searchsorted 吸附最近交易日（O(log n)，不遍历完整历史）
        idx = int(np.searchsorted(self._visible_idx, x, side="left"))
        if idx >= len(self._visible_idx):
            idx = len(self._visible_idx) - 1
        elif idx > 0:
            # 取左右更近的
            left = self._visible_idx[idx - 1]
            right = self._visible_idx[idx]
            if abs(x - left) < abs(right - x):
                idx = idx - 1
        xi = float(self._visible_idx[idx])
        dp2 = self._visible_dp2[idx] if idx < len(self._visible_dp2) else float("nan")
        m42 = self._visible_m42[idx] if idx < len(self._visible_m42) else float("nan")
        date = self._visible_dates[idx] if idx < len(self._visible_dates) else None
        locks = self._visible_locks[idx] if idx < len(self._visible_locks) else None

        # ---- 更新 crosshair ----
        self._crosshair_v.set_xdata([xi, xi])
        self._crosshair_v.set_alpha(0.9)
        self._crosshair_v.set_visible(True)

        # ---- 更新 marker ----
        if not (dp2 != dp2):  # not NaN
            self._hover_marker.set_data([xi], [dp2])
            self._hover_marker.set_alpha(1.0)
            self._hover_marker.set_visible(True)
        else:
            self._hover_marker.set_visible(False)

        # ---- 更新 tooltip ----
        text = self._build_tooltip_text(date, dp2, m42, locks)
        self._annotation.set_text(text)
        # tooltip 位置：左半边 -> 右上，右半边 -> 左上
        n = len(self._visible_idx)
        if xi < n / 2:
            self._annotation.xy = (xi, dp2 if dp2 == dp2 else 0)
            self._annotation.set_position((20, 20))
        else:
            self._annotation.xy = (xi, dp2 if dp2 == dp2 else 0)
            self._annotation.set_position((-20, 20))
        self._annotation.set_alpha(1.0)
        self._annotation.set_visible(True)

        self.canvas.draw_idle()

    def _on_axes_leave(self, event) -> None:
        """鼠标离开 axes：隐藏 crosshair / marker / tooltip。"""
        if self._crosshair_v is not None:
            self._crosshair_v.set_visible(False)
            self._crosshair_v.set_alpha(0.0)
        if self._hover_marker is not None:
            self._hover_marker.set_visible(False)
            self._hover_marker.set_alpha(0.0)
        if self._annotation is not None:
            self._annotation.set_visible(False)
            self._annotation.set_alpha(0.0)
        self.canvas.draw_idle()

    def _build_tooltip_text(
        self,
        date: Optional[_dt.date],
        dp2: float,
        m42: float,
        locks: Optional[Dict[str, Decimal]],
    ) -> str:
        """构造 tooltip 文本：日期 / D/P2 / M42 / 本周锁定阈值。"""
        lines = []
        lines.append(f"日期：{date.isoformat() if date else '--'}")
        lines.append(f"股息率2 D/P2：{fmt_yield(D(dp2), 2) if dp2 == dp2 else '--'}%")
        lines.append(f"滚动 M42：{fmt_yield(D(m42), 3) if m42 == m42 else '--'}%")
        if locks:
            lines.append(f"本周锁定 M42：{fmt_yield(locks.get('M42'), 3)}%")
            for label, k_buy, k_sell in [
                ("A仓 8%", "A_buy", "A_sell"),
                ("B仓 12%", "B_buy", "B_sell"),
                ("C仓 20%", "C_buy", "C_sell"),
            ]:
                lines.append(label)
                lines.append(f"  买：{fmt_yield(locks.get(k_buy), 3)}%")
                lines.append(f"  卖：{fmt_yield(locks.get(k_sell), 3)}%")
        return "\n".join(lines)


def _build_thresholds_config() -> dict:
    """构造 weekly_locked_thresholds_for_records 需要的最小 config。

    从默认 config 兜底加载（避免 chart_widget 直接依赖 main_window 注入）。
    """
    from ..config import load_config
    cfg = load_config()
    # 只取需要的字段
    return {
        "primary_window": cfg.get("primary_window", 42),
        "positions": cfg.get("positions", {}),
    }
