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
from decimal import Decimal, ROUND_HALF_UP
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
    detect_turning_points,
    rolling_mean,
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
        self._visible_mean42: List[float] = []
        self._visible_idx: np.ndarray = np.array([], dtype=float)
        self._visible_locks: List[Optional[Dict[str, Decimal]]] = []
        # 估算 D/P2 尾巴（官方滞后期间，运行时数据；不进 records / M42 / Mean42 / 策略）
        self._estimated_tail: List[dict] = []
        self._tail_x: np.ndarray = np.array([], dtype=float)
        self._tail_dp2: List[float] = []
        self._tail_dates: List[Optional[_dt.date]] = []
        self._tail_index_points: List[Optional[Decimal]] = []
        self._tail_kinds: List[str] = []
        # D/P2 拐点研究标记（只观察；绝不进入 M42 / Mean42 / A/B/C 策略）
        self._turning_points: List[dict] = []
        self._turning_by_date: Dict[str, dict] = {}
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
        estimated_dp2_tail: Optional[List[dict]] = None,
    ) -> None:
        self._records = records
        self._thresholds = {k: D(v) for k, v in thresholds.items() if v is not None}
        self._weekly_m42 = D(weekly_m42)
        # 估算尾巴与官方 records 始终分开保存，不合并
        self._estimated_tail = list(estimated_dp2_tail) if estimated_dp2_tail else []
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

        # 滚动 Mean42：与 M42 同 D/P2 源、同 window=42、同可见截取，用于直接对比观察
        mean42_full: List[float] = []
        for i in range(len(self._records)):
            if self._records[i].dividend_yield_2 is None:
                mean42_full.append(float("nan"))
                continue
            m = rolling_mean(full_dy2[: i + 1], 42)
            mean42_full.append(float(m) if m is not None else float("nan"))
        mean42_visible = mean42_full[-len(recs):]

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
        self._visible_mean42 = mean42_visible
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
        # Mean42 滚动点划线（与 M42 同窗口，用于对比观察，不参与策略）
        self._ax.plot(
            self._visible_idx, mean42_visible,
            label="Mean42 (42日平均数)", color=theme.MEAN42_LINE, linestyle="-.", linewidth=1.1,
        )

        # ---- 估算 D/P2 尾巴（anchor 之后官方滞后的交易日，仅观察，不进 M42/Mean42/策略） ----
        self._tail_x = np.array([], dtype=float)
        self._tail_dp2 = []
        self._tail_dates = []
        self._tail_index_points = []
        self._tail_kinds = []
        if self._estimated_tail:
            # 从最后一个【有官方 D/P2 的点】继续，保证视觉连续（首点复用官方最后点）
            last_x = float(self._visible_idx[-1]) if self._visible_idx.size else 0.0
            last_official_dp2 = self._visible_dp2[-1] if self._visible_dp2 else float("nan")
            if last_official_dp2 != last_official_dp2:  # NaN：向前找最后一个有官方 D/P2 的点
                for k in range(len(self._visible_dp2) - 1, -1, -1):
                    if self._visible_dp2[k] == self._visible_dp2[k]:
                        last_x = float(self._visible_idx[k])
                        last_official_dp2 = self._visible_dp2[k]
                        break
            tx: List[float] = [last_x]
            ty: List[float] = [last_official_dp2]
            tdates: List[Optional[_dt.date]] = [
                self._visible_dates[-1] if self._visible_dates else None
            ]
            tindex: List[Optional[Decimal]] = [None]
            tkinds: List[str] = ["official"]
            for i, pt in enumerate(self._estimated_tail):
                tx.append(last_x + (i + 1))
                ty.append(float(pt["dp2"]))
                pd = pt["date"]
                tdates.append(_dt.date.fromisoformat(pd) if isinstance(pd, str) else pd)
                tindex.append(pt["index_point"] if isinstance(pt["index_point"], Decimal) else D(pt["index_point"]))
                tkinds.append(pt["kind"])
            self._tail_x = np.array(tx, dtype=float)
            self._tail_dp2 = ty
            self._tail_dates = tdates
            self._tail_index_points = tindex
            self._tail_kinds = tkinds
            self._ax.plot(
                self._tail_x, ty,
                label="估算 D/P2", color=theme.WARNING, linestyle="--", marker="o", linewidth=1.4,
            )

        # ---- D/P2 拐点研究标记（只 scatter，不进入任何策略） ----
        self._draw_turning_points(dates)

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

    def _collect_turning_points(self) -> List[dict]:
        """收集要在图上展示的拐点。

        - 官方历史拐点：只用官方 records 检测，source="official"。
        - 若存在 estimated_dp2_tail，再在“官方 + 估算尾巴”合并序列上检测一次；
          凡是在官方序列中没有出现、只有加入估算尾巴后才出现/确认的拐点，
          标记 source="estimated"（hover 会显示“估算数据确认”）。
        """
        official_values = [
            r for r in self._records
            if getattr(r, "dividend_yield_2", None) is not None
        ]
        official_tps = detect_turning_points(official_values)
        result: List[dict] = []
        for tp in official_tps:
            tp["source"] = "official"
            result.append(tp)
        if not self._estimated_tail:
            return result

        # 官方历史拐点已经固定，估算尾巴只负责追加“官方数据尚未确认、只有接上估算
        # 尾巴后才出现/确认”的新拐点；绝不因后续估算数据反过来删除官方已确认拐点。
        combined_values: List[dict] = [
            {
                "date": r.date.isoformat() if isinstance(r.date, _dt.date) else str(r.date),
                "dp2": r.dividend_yield_2,
            }
            for r in official_values
        ]
        for pt in self._estimated_tail:
            combined_values.append({
                "date": pt["date"],
                "dp2": pt["dp2"],
            })
        combined_tps = detect_turning_points(combined_values)
        official_keys = {
            (tp["kind"], tp["pivot_date"], str(tp["pivot_dp2"]))
            for tp in official_tps
        }
        for tp in combined_tps:
            key = (tp["kind"], tp["pivot_date"], str(tp["pivot_dp2"]))
            if key not in official_keys:
                tp["source"] = "estimated"
                result.append(tp)
        return result

    def _draw_turning_points(self, dates: List[_dt.date]) -> None:
        """在现有 D/P2 曲线上追加顶/底拐点 scatter。

        顶拐点 marker="v"，底拐点 marker="^"。
        估算尾巴确认的拐点用空心 marker 区分，且不新增图例项。
        """
        self._turning_points = self._collect_turning_points()
        self._turning_by_date = {
            tp["pivot_date"]: tp
            for tp in self._turning_points
        }

        visible_index = {d.isoformat(): i for i, d in enumerate(dates)}
        tail_index: Dict[str, float] = {}
        if self._tail_dates:
            for i in range(1, len(self._tail_dates)):
                d = self._tail_dates[i]
                if d is not None:
                    tail_index[d.isoformat()] = float(self._tail_x[i])

        peak_x: List[float] = []
        peak_y: List[float] = []
        peak_est_x: List[float] = []
        peak_est_y: List[float] = []
        trough_x: List[float] = []
        trough_y: List[float] = []
        trough_est_x: List[float] = []
        trough_est_y: List[float] = []
        for tp in self._turning_points:
            pivot_date = tp["pivot_date"]
            if pivot_date in visible_index:
                x = float(visible_index[pivot_date])
            elif pivot_date in tail_index:
                x = tail_index[pivot_date]
            else:
                continue
            y = float(tp["pivot_dp2"])
            est = tp.get("source") == "estimated"
            if tp["kind"] == "peak":
                (peak_est_x if est else peak_x).append(x)
                (peak_est_y if est else peak_y).append(y)
            else:
                (trough_est_x if est else trough_x).append(x)
                (trough_est_y if est else trough_y).append(y)

        if peak_x:
            self._ax.scatter(
                peak_x, peak_y,
                marker="v", s=90, color=theme.TURN_PEAK,
                edgecolors=theme.APP_BG, linewidths=0.8, zorder=8,
                label="D/P2 顶拐点",
            )
        if peak_est_x:
            self._ax.plot(
                peak_est_x, peak_est_y,
                marker="v", linestyle="none", color=theme.TURN_PEAK,
                markerfacecolor="none", markersize=10, markeredgewidth=1.2, zorder=8,
            )
        if trough_x:
            self._ax.scatter(
                trough_x, trough_y,
                marker="^", s=90, color=theme.TURN_TROUGH,
                edgecolors=theme.APP_BG, linewidths=0.8, zorder=8,
                label="D/P2 底拐点",
            )
        if trough_est_x:
            self._ax.plot(
                trough_est_x, trough_est_y,
                marker="^", linestyle="none", color=theme.TURN_TROUGH,
                markerfacecolor="none", markersize=10, markeredgewidth=1.2, zorder=8,
            )

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
        # 估算 D/P2 尾巴区域：x 超过最后一个官方点 -> 走尾巴 Hover（不改动官方点逻辑）
        last_x = float(self._visible_idx[-1]) if self._visible_idx.size else 0.0
        if self._estimated_tail and self._tail_x.size and x > last_x:
            self._handle_tail_hover(x)
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
        mean42 = self._visible_mean42[idx] if idx < len(self._visible_mean42) else float("nan")
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
        turning = self._turning_by_date.get(date.isoformat()) if date is not None else None
        text = self._build_tooltip_text(date, dp2, m42, locks, mean42, turning=turning)
        self._annotation.set_text(text)
        # 先置可见再定位：不可见文本不会参与 get_window_extent 排版，
        # 首帧测量 bbox 需要 annotation 已处于可见状态。
        self._annotation.set_alpha(1.0)
        self._annotation.set_visible(True)
        # 锚点优先级：D/P2 -> M42 -> y 轴中点（NaN 时保证锚点仍在可见区域）
        if dp2 == dp2:
            anchor_y = dp2
        elif m42 == m42:
            anchor_y = m42
        else:
            y_min, y_max = self._ax.get_ylim()
            anchor_y = (y_min + y_max) / 2.0
        self._place_tooltip(xi, anchor_y)

        self.canvas.draw_idle()

    def _handle_tail_hover(self, x: float) -> None:
        """估算尾巴 Hover：吸附最近估算点 + 更新 crosshair/marker/tooltip。

        只负责尾巴的展示；官方点逻辑见 _on_motion 主路径。
        """
        if self._crosshair_v is None or self._hover_marker is None or self._annotation is None:
            return
        # 吸附最近估算点（与官方路径相同的 searchsorted + 就近逻辑）
        j = int(np.searchsorted(self._tail_x, x, side="left"))
        if j >= len(self._tail_x):
            j = len(self._tail_x) - 1
        elif j > 0:
            left = self._tail_x[j - 1]
            right = self._tail_x[j]
            if abs(x - left) < abs(right - x):
                j -= 1
        j = max(1, j)  # tail_x[0] 是复用官方最后点的视觉连接点，估算点从索引 1 起
        tx = float(self._tail_x[j])
        ty = float(self._tail_dp2[j])

        # crosshair
        self._crosshair_v.set_xdata([tx, tx])
        self._crosshair_v.set_alpha(0.9)
        self._crosshair_v.set_visible(True)
        # marker（橙色，与官方一致）
        self._hover_marker.set_data([tx], [ty])
        self._hover_marker.set_alpha(1.0)
        self._hover_marker.set_visible(True)
        # tooltip
        tail_date = self._tail_dates[j]
        turning = self._turning_by_date.get(tail_date.isoformat()) if tail_date is not None else None
        text = self._build_estimated_tooltip_text(
            self._tail_dates[j], ty, self._tail_index_points[j], self._tail_kinds[j],
            turning=turning,
        )
        self._annotation.set_text(text)
        self._annotation.set_alpha(1.0)
        self._annotation.set_visible(True)
        # 锚点用估算点自身（估算点不是官方数据，不参与 M42/Mean42）
        self._place_tooltip(tx, ty)
        self.canvas.draw_idle()

    def _place_tooltip(self, xi: float, anchor_y: float) -> None:
        """按锚点 (xi, anchor_y) 定位 tooltip：四象限避让 + Figure 边界二次 clamp。

        只负责定位（xy / position / ha / va / 边界补偿），不负责文本与业务逻辑。
        """
        self._annotation.xy = (xi, anchor_y)

        # ---- 第一级：按锚点在 axes 内的四象限决定展开方向 ----
        # 以吸附点 (xi, anchor_y) 的屏幕位置判断，而不是鼠标 Y：hover 按 X 吸附，
        # 鼠标可能在底部而 D/P2 点却在顶部，纵向必须以点本身为准。
        anchor_px = self._ax.transData.transform((xi, anchor_y))
        axes_bbox = self._ax.get_window_extent()
        axes_cx = (axes_bbox.x0 + axes_bbox.x1) / 2.0
        axes_cy = (axes_bbox.y0 + axes_bbox.y1) / 2.0
        if anchor_px[0] < axes_cx:
            offset_x, ha = 20.0, "left"  # 锚点在 axes 左半边 -> tooltip 放点右侧
        else:
            offset_x, ha = -20.0, "right"  # 锚点在 axes 右半边 -> tooltip 放点左侧
        if anchor_px[1] < axes_cy:
            offset_y, va = 20.0, "bottom"  # 锚点在 axes 下半边 -> tooltip 放点上方
        else:
            offset_y, va = -20.0, "top"  # 锚点在 axes 上半边 -> tooltip 放点下方
        self._annotation.set_position((offset_x, offset_y))
        self._annotation.set_ha(ha)
        self._annotation.set_va(va)

        # ---- 第二级：最终 bbox 与 Figure 边界 clamp（8px 安全边距） ----
        # annotation 用 textcoords="offset points"，补偿量需从 pixel 转 point。
        margin_px = 8.0
        renderer = self.canvas.get_renderer()
        tooltip_bbox = self._annotation.get_window_extent(renderer=renderer)
        figure_bbox = self.figure.bbox
        dx_px = 0.0
        dy_px = 0.0
        if tooltip_bbox.x0 < figure_bbox.x0 + margin_px:
            dx_px += (figure_bbox.x0 + margin_px) - tooltip_bbox.x0
        if tooltip_bbox.x1 > figure_bbox.x1 - margin_px:
            dx_px -= tooltip_bbox.x1 - (figure_bbox.x1 - margin_px)
        if tooltip_bbox.y0 < figure_bbox.y0 + margin_px:
            dy_px += (figure_bbox.y0 + margin_px) - tooltip_bbox.y0
        if tooltip_bbox.y1 > figure_bbox.y1 - margin_px:
            dy_px -= tooltip_bbox.y1 - (figure_bbox.y1 - margin_px)
        px_to_pt = 72.0 / self.figure.dpi
        offset_x, offset_y = self._annotation.get_position()
        self._annotation.set_position(
            (offset_x + dx_px * px_to_pt, offset_y + dy_px * px_to_pt)
        )

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
        mean42: float = float("nan"),
        turning: Optional[dict] = None,
    ) -> str:
        """构造 tooltip 文本：日期 / D/P2 / M42 / Mean42 / 本周锁定阈值 + 拐点确认信息。"""
        lines = []
        lines.append(f"日期：{date.isoformat() if date else '--'}")
        lines.append(f"股息率2 D/P2：{fmt_yield(D(dp2), 2) if dp2 == dp2 else '--'}%")
        lines.append(f"滚动 M42：{fmt_yield(D(m42), 3) if m42 == m42 else '--'}%")
        lines.append(f"滚动 Mean42：{fmt_yield(D(mean42), 3) if mean42 == mean42 else '--'}%")
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
        self._append_turning_info(lines, turning)
        return "\n".join(lines)

    def _build_estimated_tooltip_text(
        self,
        date: Optional[_dt.date],
        dp2: float,
        index_point: Optional[Decimal],
        kind: str,
        turning: Optional[dict] = None,
    ) -> str:
        """构造估算点 tooltip：日期 / 估算 D/P2 / 指数点位 / 性质 / 拐点确认信息。

        估算点不是官方数据：文案用"估算 D/P2"，绝不直接写成"官方 D/P2"。
        kind="intraday" -> 盘中估算；kind="close" -> 收盘反推估算。
        """
        lines = []
        lines.append(f"日期：{date.isoformat() if date else '--'}")
        lines.append(f"估算 D/P2：{fmt_yield(D(dp2), 3) if dp2 == dp2 else '--'}%")
        lines.append(f"指数点位：{self._fmt_point(index_point)}")
        kind_text = "盘中估算" if kind == "intraday" else "收盘反推估算"
        lines.append(f"性质：{kind_text}")
        self._append_turning_info(lines, turning)
        return "\n".join(lines)

    @staticmethod
    def _append_turning_info(lines: List[str], turning: Optional[dict]) -> None:
        """在已有 tooltip 下面追加拐点确认信息（不包含 BUY/SELL/买入/卖出字样）。"""
        if not turning:
            return
        lines.append("")
        kind_label = "顶" if turning["kind"] == "peak" else "底"
        lines.append(f"拐点：D/P2 {kind_label}")
        lines.append(f"极值日：{turning['pivot_date']}")
        lines.append(f"极值：{fmt_yield(D(turning['pivot_dp2']), 3)}%")
        lines.append(f"确认日：{turning['confirm_date']}")
        lines.append(f"确认值：{fmt_yield(D(turning['confirm_dp2']), 3)}%")
        lines.append(f"反转幅度：{fmt_yield(D(turning['reversal']), 3)}%")
        if turning.get("source") == "estimated":
            lines.append("确认性质：估算数据确认")

    @staticmethod
    def _fmt_point(value: Optional[Decimal]) -> str:
        """指数点位统一显示：Decimal/float -> ROUND_HALF_UP 到 2 位；None -> '--'。"""
        if value is None:
            return "--"
        v = D(value)
        if v is None:
            return "--"
        q = Decimal(1).scaleb(-2)
        return str(v.quantize(q, rounding=ROUND_HALF_UP))


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
