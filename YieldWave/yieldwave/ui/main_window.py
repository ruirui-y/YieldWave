"""主窗口：深色主题 + 顶栏 KPI + 本周机械交易点位卡 + 阈值表（含点位列）+ 各功能标签页。

视觉层级（按文档要求，从上到下）：
1. 今日正式操作 / 盘中估算参考
2. 当前点位 / 下一买卖点 / 距离
3. 官方 D/P2 / 估算当前 D/P2 / M42
4. A/B/C 阈值表（含买入/卖出估算点位）
5. 走势图（深色 + Hover）
6. 回测、交易记录、数据管理

两种周策略状态：
- 未锁定（PREVIEW）：只显示预览线，不产生正式 BUY/SELL 信号。
- 已锁定（LOCKED）：用本周锁定的 A/B/C 阈值产生正式信号。

盘中估算（按文档要求，与正式信号分开显示）：
- estimated_current_dp2 = anchor_dp2 * anchor_close / current_index_point
- A/B/C 估算点位 = anchor_close * anchor_dp2 / target_dp2
- 下一机械动作：根据当前点位 + 仓位状态自动找下一条可执行线
- 不修改仓位状态，最终仍需用户点击“确认已买入/卖出”
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import get_user_agent
from ..database import Database
from ..models import POS_EMPTY, POS_HOLDING, ValuationRecord, WeeklyStrategy
from ..precision import D, fmt_yield
from ..services import market_quote
from ..services.point_estimator import (
    Anchor,
    can_estimate,
    distance_to_target,
    estimate_current_dp2,
    yield_to_target_point,
)
from ..strategy import (
    ACT_BUY,
    ACT_HOLD,
    ACT_SELL,
    ACT_WAIT,
    compute_medians,
    compute_thresholds,
    current_core_percent,
    current_equity_percent,
    current_swing_percent,
    current_week_id,
    evaluate_position,
    generate_weekly_strategy,
    thresholds_from_weekly,
    valid_dp2_count,
)
from ..data_sources import honglicha
from . import theme
from .backtest_widget import BacktestWidget
from .chart_widget import ChartWidget
from .data_widget import DataWidget
from .trade_widget import TradeWidget

SIGNAL_LABEL = {
    ACT_BUY: "买入", ACT_SELL: "卖出", ACT_HOLD: "继续持有", ACT_WAIT: "空仓等待",
}
SIGNAL_ACTION_CSS = {
    ACT_BUY: "ActionBuy", ACT_SELL: "ActionSell",
    ACT_HOLD: "ActionHold", ACT_WAIT: "ActionWait",
}


def _fmt_point(value, decimals: int = 2) -> str:
    """指数点位统一显示：Decimal/float -> ROUND_HALF_UP 到 decimals 位；None -> '--'。"""
    if value is None:
        return "--"
    try:
        v = D(value)
        if v is None:
            return "--"
        q = Decimal(1).scaleb(-decimals)
        return str(v.quantize(q, rounding="ROUND_HALF_UP"))
    except Exception:
        return "--"


def _fmt_dist(value, decimals: int = 2) -> str:
    """距离点数 / 百分比带符号显示，None -> '--'。"""
    if value is None:
        return "--"
    v = D(value)
    if v is None:
        return "--"
    q = Decimal(1).scaleb(-decimals)
    s = str(v.quantize(q, rounding="ROUND_HALF_UP"))
    if not s.startswith("-"):
        s = "+" + s
    return s


class _ManualPointDialog(QDialog):
    """手工输入当前指数点位（备用方案：网络源挂了仍可使用）。"""

    def __init__(self, parent: Optional[QWidget] = None, last_point: Optional[float] = None):
        super().__init__(parent)
        self.setWindowTitle("手工输入当前指数点位")
        fl = QFormLayout(self)
        self.point = QDoubleSpinBox()
        self.point.setRange(0.0, 1_000_000.0)
        self.point.setDecimals(2)
        self.point.setSingleStep(0.01)
        if last_point and last_point > 0:
            self.point.setValue(float(last_point))
        fl.addRow("当前指数点位：", self.point)
        self.note = QLabel("网络行情失败时可手工输入，估算 D/P2 与所有买卖点位将立即重新计算。")
        self.note.setObjectName("MutedNote")
        self.note.setWordWrap(True)
        fl.addRow(self.note)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        fl.addRow(btns)


class MainWindow(QMainWindow):
    def __init__(self, db: Database, config: Dict):
        super().__init__()
        self.db = db
        self.config = config
        # 历史估值与策略状态
        self.records: List[ValuationRecord] = []
        self.medians: Dict[str, Optional[Decimal]] = {}
        self.latest: Optional[ValuationRecord] = None
        self.positions: List = []
        self.weekly: Optional[WeeklyStrategy] = None
        self.locked: bool = False
        self.thresholds: Dict[str, Decimal] = {}
        self.preview_thresholds: Dict[str, Decimal] = {}
        self.signals: Dict[str, Tuple[str, str]] = {}
        self.fetch_error: Optional[str] = None
        # 盘中估算状态（独立于正式信号）
        self.anchor: Optional[Anchor] = None
        self.current_point: Optional[Decimal] = None  # 当前指数点位（Decimal 或 None）
        self.current_quote: Optional[market_quote.CurrentQuote] = None
        self.quote_error: Optional[str] = None
        self.manual_point_override: Optional[Decimal] = None  # 手工输入的当前点位

        self.setWindowTitle("YieldWave · 中证红利低波 H30269 · 盘中机械交易助手")
        self.resize(1240, 880)
        self._build_ui()
        self._build_refresh_timer()
        self.refresh_all()
        # 启动后异步刷新一次行情（不阻塞 UI）
        QTimer.singleShot(1500, self.refresh_current_point_async)

    # ---------------- UI 构建 ----------------
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.setCentralWidget(central)

        # ---- 顶栏：标题 + 操作按钮 ----
        hdr = QHBoxLayout()
        title = QLabel("YieldWave · 中证红利低波 H30269")
        title.setObjectName("SectionTitle")
        hdr.addWidget(title)
        hdr.addStretch(1)
        self.refresh_btn = QPushButton("刷新当前点位")
        self.refresh_btn.setObjectName("PrimaryButton")
        self.refresh_btn.clicked.connect(self.refresh_current_point_async)
        hdr.addWidget(self.refresh_btn)
        self.manual_btn = QPushButton("手工输入点位")
        self.manual_btn.clicked.connect(self.manual_point)
        hdr.addWidget(self.manual_btn)
        self.lock_btn = QPushButton("锁定本周策略")
        self.lock_btn.clicked.connect(self.lock_week)
        hdr.addWidget(self.lock_btn)
        root.addLayout(hdr)

        # ---- KPI 区：官方锚点 + 当前点位 + 估算 D/P2 ----
        self.kpi_frame = QFrame()
        self.kpi_frame.setObjectName("CardFrame")
        kpi_root = QVBoxLayout(self.kpi_frame)
        kpi_root.setContentsMargins(10, 8, 10, 8)
        kpi_root.setSpacing(4)
        # 第一行：标题与本周锁定状态
        kpi_top = QHBoxLayout()
        self.kpi_title = QLabel("官方锚点 / 当前点位 / 估算 D/P2")
        self.kpi_title.setObjectName("KpiLabel")
        kpi_top.addWidget(self.kpi_title)
        kpi_top.addStretch(1)
        self.lock_status_label = QLabel("本周策略状态：--")
        self.lock_status_label.setObjectName("KpiLabel")
        kpi_top.addWidget(self.lock_status_label)
        kpi_root.addLayout(kpi_top)
        # 第二行：官方锚点行
        anchor_grid = QGridLayout()
        anchor_grid.setSpacing(8)
        self.lbl_anchor_date = self._kpi_cell(anchor_grid, 0, 0, "官方D/P2日期")
        self.lbl_anchor_dp2 = self._kpi_cell(anchor_grid, 0, 1, "官方D/P2")
        self.lbl_anchor_close = self._kpi_cell(anchor_grid, 0, 2, "锚点收盘")
        self.lbl_current_point = self._kpi_cell(anchor_grid, 0, 3, "当前点位")
        self.lbl_current_point_time = self._kpi_cell(anchor_grid, 0, 4, "更新时间")
        kpi_root.addLayout(anchor_grid)
        # 第三行：估算 D/P2 + M20/M42/M60 + 数据计数
        est_grid = QGridLayout()
        est_grid.setSpacing(8)
        self.lbl_est_dp2 = self._kpi_cell(est_grid, 0, 0, "估算当前D/P2")
        self.lbl_m20 = self._kpi_cell(est_grid, 0, 1, "M20")
        self.lbl_m42 = self._kpi_cell(est_grid, 0, 2, "M42")
        self.lbl_m60 = self._kpi_cell(est_grid, 0, 3, "M60")
        self.lbl_valid = self._kpi_cell(est_grid, 0, 4, "有效数据")
        kpi_root.addLayout(est_grid)
        root.addWidget(self.kpi_frame)

        # ---- 本周机械交易点位卡 ----
        self.mech_frame = QFrame()
        self.mech_frame.setObjectName("CardFrame")
        mech_root = QVBoxLayout(self.mech_frame)
        mech_root.setContentsMargins(10, 8, 10, 8)
        mech_root.setSpacing(4)
        self.mech_title = QLabel("本周机械交易点位")
        self.mech_title.setObjectName("SectionTitle")
        mech_root.addWidget(self.mech_title)
        self.mech_buy_label = QLabel("买入区（股息率升 / 指数降 → 越接近买线越值得加仓）")
        self.mech_buy_label.setObjectName("KpiLabel")
        mech_root.addWidget(self.mech_buy_label)
        self.mech_buy_table = QTableWidget()
        self.mech_buy_table.setColumnCount(4)
        self.mech_buy_table.setHorizontalHeaderLabels(["仓位", "买入D/P2", "买入估算点位", "当前距离"])
        self.mech_buy_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mech_buy_table.verticalHeader().setVisible(False)
        mech_root.addWidget(self.mech_buy_table)
        self.mech_sell_label = QLabel("卖出区（股息率降 / 指数升 → 越接近卖线越值得减仓）")
        self.mech_sell_label.setObjectName("KpiLabel")
        mech_root.addWidget(self.mech_sell_label)
        self.mech_sell_table = QTableWidget()
        self.mech_sell_table.setColumnCount(4)
        self.mech_sell_table.setHorizontalHeaderLabels(["仓位", "卖出D/P2", "卖出估算点位", "当前距离"])
        self.mech_sell_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mech_sell_table.verticalHeader().setVisible(False)
        mech_root.addWidget(self.mech_sell_table)
        # 当前点位 + 下一动作 + 距离（醒目）
        self.mech_next_label = QLabel("下一机械动作：--")
        self.mech_next_label.setObjectName("KpiValueEmphasis")
        mech_root.addWidget(self.mech_next_label)
        root.addWidget(self.mech_frame)

        # ---- 今日操作（正式信号 / 盘中参考） ----
        self.action_box = QGroupBox("今日操作（正式信号 + 盘中参考）")
        self.action_layout = QVBoxLayout(self.action_box)
        root.addWidget(self.action_box)

        # ---- 阈值表（加点位列） ----
        self.thr_table = QTableWidget()
        self.thr_table.setColumnCount(9)
        self.thr_table.setHorizontalHeaderLabels([
            "仓位", "状态",
            "买入D/P2", "买入点位", "卖出D/P2", "卖出点位",
            "当前D/P2", "当前点位", "动作",
        ])
        self.thr_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.thr_table.verticalHeader().setVisible(False)
        root.addWidget(self.thr_table)

        # ---- 标签页：走势图 / 回测 / 交易记录 / 数据管理 ----
        tabs = QTabWidget()
        self.chart_w = ChartWidget()
        self.backtest_w = BacktestWidget(self.db, self.config, on_config_changed=self.reload_config)
        self.trade_w = TradeWidget(
            self.db, self.config,
            get_signals=self.get_signals,
            get_weekly=lambda: self.weekly,
            on_changed=self.refresh_all,
        )
        self.data_w = DataWidget(self.db, self.config, on_update=self.do_update, on_changed=self.refresh_all)
        tabs.addTab(self.chart_w, "走势图")
        tabs.addTab(self.backtest_w, "回测/优化")
        tabs.addTab(self.trade_w, "交易记录")
        tabs.addTab(self.data_w, "数据管理")
        root.addWidget(tabs, stretch=1)

    @staticmethod
    def _kpi_cell(grid: QGridLayout, row: int, col: int, label_text: str) -> QLabel:
        """在 grid 里放一个 KPI 单元（label + value），返回 value QLabel。"""
        cell = QVBoxLayout()
        cell.setSpacing(2)
        lbl = QLabel(label_text)
        lbl.setObjectName("KpiLabel")
        val = QLabel("--")
        val.setObjectName("KpiValue")
        cell.addWidget(lbl)
        cell.addWidget(val)
        wrapper = QWidget()
        wrapper.setLayout(cell)
        grid.addWidget(wrapper, row, col)
        return val

    def _build_refresh_timer(self) -> None:
        """行情自动刷新（默认 60s）。失败不影响 UI。"""
        mq = self.config.get("market_quote", {})
        enabled = mq.get("enabled", True)
        if not enabled:
            return
        seconds = int(mq.get("refresh_seconds", 60))
        seconds = max(seconds, 30)  # 最低 30s，避免高频
        self._quote_timer = QTimer(self)
        self._quote_timer.timeout.connect(self.refresh_current_point_async)
        self._quote_timer.start(seconds * 1000)

    # ---------------- 数据刷新 ----------------
    def refresh_all(self) -> None:
        self.records = self.db.get_all_valuations()
        self.medians = compute_medians(self.records, self.config["windows"])
        self.latest = self.db.get_latest()
        self.positions = self.db.get_positions()
        self.weekly = self.db.get_weekly_strategy(current_week_id())
        self.locked = self.weekly is not None
        # 锚点：最近一个有 D/P2 的记录（来自红利查官方） + 该日 H30269 收盘（缓存）
        self._update_anchor()
        self._update_current_point()
        self.update_signals()
        self._update_kpi()
        self._update_mech_card()
        self._update_action_box()
        self._update_threshold_table()
        # 子组件刷新
        self.chart_w.set_data(
            self.records,
            self.thresholds if self.locked else self.preview_thresholds,
            weekly_m42=(self.weekly.m42 if self.weekly else None),
        )
        self.trade_w.refresh()
        self.data_w.refresh_stats()

    def _update_anchor(self) -> None:
        """从 self.latest 取 anchor_date / anchor_dp2；anchor_close 取缓存或网络。"""
        if self.latest is None or self.latest.dividend_yield_2 is None:
            self.anchor = None
            return
        anchor_date = self.latest.date.isoformat()
        anchor_dp2 = self.latest.dividend_yield_2
        # 优先用缓存
        cached = self.db.get_index_close(anchor_date)
        if cached is not None:
            self.anchor = Anchor(date=anchor_date, dp2=anchor_dp2, close=D(cached))
            return
        # 缓存缺：尝试抓单日（异步避免阻塞；这里只 fire-and-forget，结果再 refresh）
        self.anchor = Anchor(date=anchor_date, dp2=anchor_dp2, close=None)
        # 异步补抓 anchor_close（不阻塞 UI）
        QTimer.singleShot(200, lambda: self._fetch_anchor_close_async(anchor_date))

    def _fetch_anchor_close_async(self, anchor_date: str) -> None:
        """后台拉取 anchor_date 当天 close，缓存进 DB 后刷新 UI。"""
        from ..services.market_quote import fetch_close_on

        def work():
            try:
                close, err = fetch_close_on(anchor_date)
                if close is not None:
                    self.db.upsert_index_close(anchor_date, close)
                return close, err
            except Exception as exc:
                return None, f"anchor_close 抓取失败：{exc}"

        from PyQt6.QtCore import QThread, pyqtSignal

        class _Worker(QThread):
            done = pyqtSignal(object, object)

            def __init__(self, fn):
                super().__init__()
                self._fn = fn

            def run(self):
                try:
                    c, e = self._fn()
                    self.done.emit(c, e)
                except Exception as exc:
                    self.done.emit(None, str(exc))

        self._anchor_worker = _Worker(work)
        self._anchor_worker.done.connect(self._on_anchor_close_fetched)
        self._anchor_worker.start()

    def _on_anchor_close_fetched(self, close, err) -> None:
        if close is not None and self.anchor is not None:
            self.anchor = Anchor(date=self.anchor.date, dp2=self.anchor.dp2, close=D(close))
            self._update_kpi()
            self._update_mech_card()
            self._update_action_box()
            self._update_threshold_table()
            self.chart_w.set_data(
                self.records,
                self.thresholds if self.locked else self.preview_thresholds,
                weekly_m42=(self.weekly.m42 if self.weekly else None),
            )
        elif err:
            print(f"[行情] anchor_close 抓取失败：{err}", flush=True)

    def _update_current_point(self) -> None:
        """优先级：手工输入 > 最近一次成功抓取 > None。"""
        if self.manual_point_override is not None:
            self.current_point = self.manual_point_override
            return
        q = self.db.get_last_quote()
        self.current_quote = q
        if q and q.current is not None and q.current > 0:
            self.current_point = D(q.current)
        else:
            self.current_point = None

    def refresh_current_point_async(self) -> None:
        """异步刷新当前盘中点位（不阻塞 UI）。"""
        from PyQt6.QtCore import QThread, pyqtSignal
        from ..services.market_quote import fetch_current_quote

        def work():
            try:
                return fetch_current_quote()
            except Exception as exc:
                return None, f"行情抓取失败：{exc}"

        class _Worker(QThread):
            done = pyqtSignal(object, object)

            def __init__(self, fn):
                super().__init__()
                self._fn = fn

            def run(self):
                try:
                    q, e = self._fn()
                    self.done.emit(q, e)
                except Exception as exc:
                    self.done.emit(None, str(exc))

        self._quote_worker = _Worker(work)
        self._quote_worker.done.connect(self._on_current_point_fetched)
        self._quote_worker.start()

    def _on_current_point_fetched(self, quote, err) -> None:
        if quote is not None and getattr(quote, "current", None) is not None:
            self.db.save_last_quote(quote)
            # 如果今天没缓存 close，则同步缓存今天的 current 作为 today close 的占位
            today_iso = _dt.date.today().isoformat()
            if self.db.get_index_close(today_iso) is None:
                # 注：盘中 current 不等于收盘 close，仅作兜底；收盘后会重新抓 close
                pass
            self.current_quote = quote
            self.quote_error = None
            # 手工 override 仍优先
            if self.manual_point_override is None:
                self.current_point = D(quote.current)
        else:
            self.quote_error = err or "行情抓取失败，使用最后一次成功数据"
        self._update_kpi()
        self._update_mech_card()
        self._update_action_box()
        self._update_threshold_table()

    def update_signals(self) -> None:
        m42 = self.medians.get("M42")
        self.preview_thresholds = compute_thresholds(m42, self.config) if m42 is not None else {}
        if self.locked and self.weekly is not None:
            self.thresholds = thresholds_from_weekly(self.weekly)
        else:
            self.thresholds = {}
        cur = self.latest.dividend_yield_2 if self.latest else None
        self.signals = {}
        for p in self.positions:
            if p.kind == "core":
                self.signals[p.name] = (ACT_WAIT, "核心仓：仅手动确认建仓，无自动信号")
                continue
            if cur is None or not self.thresholds:
                self.signals[p.name] = (ACT_WAIT, "本周未锁定，不产生正式信号（仅预览）")
            else:
                act, reason = evaluate_position(p, cur, self.thresholds)
                self.signals[p.name] = (act, reason)

    def current_thresholds(self) -> Dict[str, Decimal]:
        return self.thresholds

    def get_signals(self) -> Dict[str, Tuple[str, str]]:
        return self.signals

    def reload_config(self) -> None:
        from ..config import load_config
        self.config = load_config()
        self.refresh_all()

    # ---------------- 展示 ----------------
    def _fmt(self, v: object, decimals: int = 3) -> str:
        """股息率/阈值统一显示：Decimal 用 ROUND_HALF_UP 量化到 decimals 位；None 显示 '--'。"""
        if v is None:
            return "--"
        if isinstance(v, Decimal):
            return fmt_yield(v, decimals)
        d = D(v)
        return fmt_yield(d, decimals) if d is not None else "--"

    def _update_kpi(self) -> None:
        valid = valid_dp2_count(self.records)
        n = len(self.records)
        warm = ""
        if valid < self.config["primary_window"]:
            warm = f"  ⚠️ 有效D/P2热身中：{valid} / {self.config['primary_window']} 个交易日"
        # 官方锚点
        if self.latest is not None and self.latest.dividend_yield_2 is not None:
            self.lbl_anchor_date.setText(self.latest.date.isoformat())
            self.lbl_anchor_dp2.setText(f"{self._fmt(self.latest.dividend_yield_2, 2)}%")
        else:
            self.lbl_anchor_date.setText("--")
            self.lbl_anchor_dp2.setText("--")
        # anchor_close
        if self.anchor is not None and self.anchor.close is not None:
            self.lbl_anchor_close.setText(_fmt_point(self.anchor.close))
        else:
            self.lbl_anchor_close.setText("-- (抓取中)")
        # 当前点位 + 更新时间
        if self.current_point is not None:
            self.lbl_current_point.setText(_fmt_point(self.current_point))
        elif self.current_quote is not None and self.current_quote.current is not None:
            self.lbl_current_point.setText(_fmt_point(self.current_quote.current))
        else:
            self.lbl_current_point.setText("--")
        if self.current_quote is not None:
            self.lbl_current_point_time.setText(
                f"{self.current_quote.trade_date} {self.current_quote.trade_time}"
            )
        else:
            self.lbl_current_point_time.setText("--")
        # 估算 D/P2 + 中位数
        est_dp2 = estimate_current_dp2(
            self.anchor.dp2 if self.anchor else None,
            self.anchor.close if self.anchor else None,
            self.current_point,
        )
        if est_dp2 is not None:
            self.lbl_est_dp2.setText(f"{self._fmt(est_dp2, 3)}%  [估算]")
            self.lbl_est_dp2.setToolTip(
                "根据最近官方 D/P2 及同日指数收盘点位静态折算。"
                "假设短期分红基数不变，仅用于盘中交易参考。"
            )
        else:
            reason = "缺 anchor / 当前点位"
            if self.anchor is None or not self.anchor.valid():
                reason = "缺少官方锚点"
            elif self.current_point is None or self.current_point <= 0:
                reason = "缺少当前指数点位"
            self.lbl_est_dp2.setText(f"-- [估算] ({reason})")
            self.lbl_est_dp2.setToolTip(
                "根据最近官方 D/P2 及同日指数收盘点位静态折算。"
                "缺少锚点或当前点位时无法估算。"
            )
        self.lbl_m20.setText(f"{self._fmt(self.medians.get('M20'))}%")
        self.lbl_m42.setText(f"{self._fmt(self.medians.get('M42'))}%")
        self.lbl_m60.setText(f"{self._fmt(self.medians.get('M60'))}%")
        self.lbl_valid.setText(f"{valid} / 总 {n} 条{warm}")
        # 本周策略状态
        if self.locked:
            self.lock_status_label.setText(
                f"本周已锁定 · M42={self._fmt(self.weekly.m42, 3)}%"
            )
        else:
            self.lock_status_label.setText("本周未锁定（仅预览，不产生正式信号）")

    def _next_mech_action(self) -> Optional[Tuple[str, str, Decimal, Optional[Decimal], str]]:
        """根据当前点位 + 仓位状态找下一条可执行线。

        返回 (position_name, action_label, target_point, distance, reason) 或 None。
        - EMPTY 仓位 -> 只看 buy 线（点位越低越接近买）；
        - HOLDING 仓位 -> 只看 sell 线（点位越高越接近卖）；
        - 已越过对应点位 -> "已进入 X 区"。
        """
        if self.anchor is None or not self.anchor.valid():
            return None
        if self.current_point is None or self.current_point <= 0:
            return None
        # 当前活跃阈值：本周锁定优先；未锁定用预览（明确标"预览"）
        use = self.thresholds if self.locked else self.preview_thresholds
        if not use:
            return None
        # 收集所有候选（点位与目标）
        candidates = []
        for p in self.positions:
            if p.kind == "core":
                continue
            if p.status == POS_EMPTY:
                # 只考虑买入线
                target_dp2 = use.get(f"{p.name}_buy")
                if target_dp2 is None:
                    continue
                target_pt = yield_to_target_point(
                    self.anchor.dp2, self.anchor.close, target_dp2
                )
                if target_pt is None or target_pt <= 0:
                    continue
                candidates.append((p, "买入", target_dp2, target_pt, "buy"))
            elif p.status == POS_HOLDING:
                target_dp2 = use.get(f"{p.name}_sell")
                if target_dp2 is None:
                    continue
                target_pt = yield_to_target_point(
                    self.anchor.dp2, self.anchor.close, target_dp2
                )
                if target_pt is None or target_pt <= 0:
                    continue
                candidates.append((p, "卖出", target_dp2, target_pt, "sell"))
        if not candidates:
            return None
        # EMPTY：找点位最高的 buy 线（最接近 current，即最可能先触发）
        #   因为 buy 线点位 < current_point 时 = "已进入买区"，应当选最高（最接近）那条
        # HOLDING：找点位最低的 sell 线（最接近 current）
        #   sell 线点位 > current_point 时 = "已进入卖区"，应选最低那条
        # 统一：按 |target_pt - current_point| 最小的候选
        cur = self.current_point
        best = min(candidates, key=lambda c: abs(c[3] - cur))
        p, action, target_dp2, target_pt, kind = best
        dist = distance_to_target(cur, target_pt)
        dist_str = (
            f"{_fmt_dist(dist[0])} 点 / {_fmt_dist(dist[1])}%"
            if dist else "--"
        )
        # 是否已越过
        crossed = (kind == "buy" and cur <= target_pt) or (kind == "sell" and cur >= target_pt)
        if crossed:
            label = f"已进入 {p.label}{action}参考区 · 目标点位 {target_pt.quantize(Decimal('0.01'))}"
        else:
            label = f"{p.label}{action} {p.percent:.0f}% · 目标点位 {target_pt.quantize(Decimal('0.01'))}"
        note = "预览（本周未锁定）" if not self.locked else "本周锁定"
        return p.name, label, target_pt, dist[0] if dist else None, note

    def _update_mech_card(self) -> None:
        """本周机械交易点位卡。"""
        use = self.thresholds if self.locked else self.preview_thresholds
        # 买入区
        swing = [p for p in self.positions if p.kind != "core"]
        buy_rows = [p for p in swing if p.name in self.config["positions"]]
        # 顺序：C -> B -> A（点位从低到高，对应"越跌越买"档位递进）
        buy_rows = sorted(buy_rows, key=lambda p: -self.config["positions"][p.name].get("percent", 0))
        self.mech_buy_table.setRowCount(len(buy_rows))
        for i, p in enumerate(buy_rows):
            target_dp2 = use.get(f"{p.name}_buy")
            target_pt = yield_to_target_point(
                self.anchor.dp2 if self.anchor else None,
                self.anchor.close if self.anchor else None,
                target_dp2,
            ) if target_dp2 is not None else None
            dist = distance_to_target(self.current_point, target_pt) if target_pt is not None else None
            self.mech_buy_table.setItem(i, 0, QTableWidgetItem(f"{p.label} {p.percent:.0f}%"))
            self.mech_buy_table.setItem(i, 1, QTableWidgetItem(f"{self._fmt(target_dp2, 3)}%"))
            self.mech_buy_table.setItem(i, 2, QTableWidgetItem(_fmt_point(target_pt)))
            dist_text = (
                f"{_fmt_dist(dist[0])} 点 / {_fmt_dist(dist[1])}%"
                if dist else "--"
            )
            self.mech_buy_table.setItem(i, 3, QTableWidgetItem(dist_text))
        # 卖出区：A -> B -> C（点位从低到高，"越涨越卖"档位递进）
        sell_rows = sorted(swing, key=lambda p: self.config["positions"][p.name].get("percent", 0))
        self.mech_sell_table.setRowCount(len(sell_rows))
        for i, p in enumerate(sell_rows):
            target_dp2 = use.get(f"{p.name}_sell")
            target_pt = yield_to_target_point(
                self.anchor.dp2 if self.anchor else None,
                self.anchor.close if self.anchor else None,
                target_dp2,
            ) if target_dp2 is not None else None
            dist = distance_to_target(self.current_point, target_pt) if target_pt is not None else None
            self.mech_sell_table.setItem(i, 0, QTableWidgetItem(f"{p.label} {p.percent:.0f}%"))
            self.mech_sell_table.setItem(i, 1, QTableWidgetItem(f"{self._fmt(target_dp2, 3)}%"))
            self.mech_sell_table.setItem(i, 2, QTableWidgetItem(_fmt_point(target_pt)))
            dist_text = (
                f"{_fmt_dist(dist[0])} 点 / {_fmt_dist(dist[1])}%"
                if dist else "--"
            )
            self.mech_sell_table.setItem(i, 3, QTableWidgetItem(dist_text))
        # 下一机械动作
        nxt = self._next_mech_action()
        if nxt is None:
            cur_text = f"当前指数：{_fmt_point(self.current_point)}"
            if self.anchor is None or not self.anchor.valid():
                cur_text += "（缺官方锚点，无法估算下一动作）"
            elif self.current_point is None:
                cur_text += "（缺当前点位，请刷新行情或手工输入）"
            elif not use:
                cur_text += "（本周策略未锁定，且无预览线）"
            self.mech_next_label.setText(cur_text)
        else:
            _name, label, target_pt, dist_val, note = nxt
            cur_text = (
                f"下一动作：{label}  当前点位：{_fmt_point(self.current_point)}  "
                f"距离：{_fmt_dist(dist_val)} 点"
                f"  [{note}]"
            )
            self.mech_next_label.setText(cur_text)

    def _update_action_box(self) -> None:
        while self.action_layout.count():
            item = self.action_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        cur_dp2 = self.latest.dividend_yield_2 if self.latest else None
        est_dp2 = estimate_current_dp2(
            self.anchor.dp2 if self.anchor else None,
            self.anchor.close if self.anchor else None,
            self.current_point,
        )

        # 第一栏：正式信号
        sec1 = QLabel("正式信号（基于官方 D/P2）")
        sec1.setObjectName("SectionTitle")
        self.action_layout.addWidget(sec1)
        if not self.locked:
            wait = QLabel("今日正式操作：等待锁定本周策略（未锁定前不产生正式 BUY/SELL 信号）")
            wait.setObjectName("WarnNote")
            self.action_layout.addWidget(wait)
            prev = QLabel("预览买卖线（非正式信号，锁定后生效）：")
            prev.setObjectName("MutedNote")
            self.action_layout.addWidget(prev)
            for name in self.config["positions"]:
                line = QLabel(
                    f"{self.config['positions'][name]['label']} {self.config['positions'][name]['percent']:.0f}%："
                    f"买 {self._fmt(self.preview_thresholds.get(f'{name}_buy'))} / "
                    f"卖 {self._fmt(self.preview_thresholds.get(f'{name}_sell'))}"
                )
                line.setObjectName("MutedNote")
                self.action_layout.addWidget(line)
        else:
            for p in self.positions:
                if p.kind == "core":
                    continue
                act, reason = self.signals.get(p.name, (ACT_WAIT, ""))
                line = QLabel(
                    f"{p.label} {p.percent:.0f}%：{SIGNAL_LABEL[act]}    （{reason}）"
                )
                line.setObjectName(SIGNAL_ACTION_CSS[act])
                self.action_layout.addWidget(line)

        # 第二栏：盘中估算参考
        sec2 = QLabel("盘中估算参考（基于指数点位静态折算 D/P2，不修改仓位状态）")
        sec2.setObjectName("SectionTitle")
        self.action_layout.addWidget(sec2)
        if est_dp2 is not None:
            est_label = QLabel(
                f"估算当前 D/P2：{self._fmt(est_dp2, 3)}%  [估算]"
                f"    官方 D/P2：{self._fmt(cur_dp2, 2)}%"
            )
            est_label.setObjectName("ActionHold")
            est_label.setToolTip(
                "公式：estimated_current_dp2 = anchor_dp2 * anchor_close / current_index_point"
                "；假设短期分红基数不变，仅用于盘中参考，不是官方实时 D/P2。"
            )
            self.action_layout.addWidget(est_label)
            nxt = self._next_mech_action()
            if nxt:
                _n, label, _tp, _d, note = nxt
                nxt_label = QLabel(f"下一机械动作：{label}  [{note}]")
                nxt_label.setObjectName("ActionBuy" if "买" in label else "ActionSell")
                self.action_layout.addWidget(nxt_label)
            else:
                hint = QLabel("（缺少 anchor_close 或当前点位，无法估算下一动作；请等待抓取或手工输入）")
                hint.setObjectName("MutedNote")
                self.action_layout.addWidget(hint)
        else:
            hint = QLabel("（缺少 anchor 或当前指数点位，无法估算盘中 D/P2。请等待抓取或手工输入。）")
            hint.setObjectName("MutedNote")
            self.action_layout.addWidget(hint)
        if self.quote_error:
            err = QLabel(f"⚠️ {self.quote_error}，使用最后一次成功数据。")
            err.setObjectName("WarnNote")
            self.action_layout.addWidget(err)

        self._append_summary()

    def _append_summary(self) -> None:
        core = current_core_percent(self.positions)
        swing = current_swing_percent(self.positions)
        equity = current_equity_percent(self.positions)
        summary = QLabel(
            f"当前实际仓位：核心 {core:.0f}%（已建） + 波段 {swing:.0f}%（已持有）"
            f" = 实际权益 {equity:.0f}%"
        )
        summary.setObjectName("WarnNote")
        self.action_layout.addWidget(summary)
        note = QLabel(
            f"（数据日期：{self.latest.date.isoformat() if self.latest else '-'}；"
            f"核心仓 build_percentile=50/65/80 为待回测确认初值；"
            f"程序只产生信号，不自动下单。投资有风险，历史回测不代表未来收益。）"
        )
        note.setObjectName("MutedNote")
        note.setWordWrap(True)
        self.action_layout.addWidget(note)

    def _update_threshold_table(self) -> None:
        swing = [p for p in self.positions if p.kind != "core"]
        # 顺序：A -> B -> C
        swing = sorted(swing, key=lambda p: self.config["positions"][p.name].get("percent", 0))
        self.thr_table.setRowCount(len(swing))
        cur_dp2 = self.latest.dividend_yield_2 if self.latest else None
        est_dp2 = estimate_current_dp2(
            self.anchor.dp2 if self.anchor else None,
            self.anchor.close if self.anchor else None,
            self.current_point,
        )
        use = self.thresholds if self.locked else self.preview_thresholds
        for i, p in enumerate(swing):
            act, _ = self.signals.get(p.name, (ACT_WAIT, ""))
            action_text = SIGNAL_LABEL[act] if self.locked else "预览"
            # 仓位 + 状态
            self.thr_table.setItem(i, 0, QTableWidgetItem(f"{p.label} {p.percent:.0f}%"))
            self.thr_table.setItem(i, 1, QTableWidgetItem(p.status))
            # 买入 D/P2 + 买入点位
            buy_dp2 = use.get(f"{p.name}_buy")
            buy_pt = yield_to_target_point(
                self.anchor.dp2 if self.anchor else None,
                self.anchor.close if self.anchor else None,
                buy_dp2,
            ) if buy_dp2 is not None else None
            self.thr_table.setItem(i, 2, QTableWidgetItem(f"{self._fmt(buy_dp2, 3)}%"))
            buy_pt_text = _fmt_point(buy_pt)
            if not self.locked and buy_pt is not None:
                buy_pt_text += " [预览]"
            self.thr_table.setItem(i, 3, QTableWidgetItem(buy_pt_text))
            # 卖出 D/P2 + 卖出点位
            sell_dp2 = use.get(f"{p.name}_sell")
            sell_pt = yield_to_target_point(
                self.anchor.dp2 if self.anchor else None,
                self.anchor.close if self.anchor else None,
                sell_dp2,
            ) if sell_dp2 is not None else None
            self.thr_table.setItem(i, 4, QTableWidgetItem(f"{self._fmt(sell_dp2, 3)}%"))
            sell_pt_text = _fmt_point(sell_pt)
            if not self.locked and sell_pt is not None:
                sell_pt_text += " [预览]"
            self.thr_table.setItem(i, 5, QTableWidgetItem(sell_pt_text))
            # 当前 D/P2（官方 + 估算都显示）
            cur_text = f"{self._fmt(cur_dp2, 2)}%"
            if est_dp2 is not None:
                cur_text += f"\n≈{self._fmt(est_dp2, 3)}% [估算]"
            self.thr_table.setItem(i, 6, QTableWidgetItem(cur_text))
            # 当前点位
            self.thr_table.setItem(i, 7, QTableWidgetItem(_fmt_point(self.current_point)))
            # 动作
            act_item = QTableWidgetItem(action_text)
            if self.locked:
                # SIGNAL_ACTION_CSS[act] 形如 "ActionBuy" -> 取对应 theme 颜色（BUY/SELL/HOLD/WAIT）
                color_key = SIGNAL_ACTION_CSS[act].replace("Action", "").upper()
                act_item.setForeground(QColor(getattr(theme, color_key)))
            else:
                act_item.setForeground(QColor(theme.TEXT_MUTED))
            self.thr_table.setItem(i, 8, act_item)

    # ---------------- 动作 ----------------
    def lock_week(self) -> None:
        m42 = self.medians.get("M42")
        if m42 is None:
            QMessageBox.warning(self, "无法锁定", "数据不足，无法计算 M42。")
            return
        existing = self.db.get_weekly_strategy(current_week_id())
        if existing is not None:
            QMessageBox.information(
                self, "本周已锁定",
                f"本周（{existing.week_id}）策略已锁定，不能重复锁定。\n"
                f"如需强制覆盖，请使用开发/维护模式（force=True）。"
            )
            return
        ws = generate_weekly_strategy(m42, self.config)
        self.db.save_weekly_strategy(ws)
        self.weekly = ws
        self.locked = True
        self.update_signals()
        self.refresh_all()
        QMessageBox.information(
            self, "已锁定",
            f"本周（{ws.week_id}）策略已锁定：\nM42={fmt_yield(ws.m42, 3)}%\n"
            f"A 买{fmt_yield(ws.a_buy, 3)}/卖{fmt_yield(ws.a_sell, 3)}  "
            f"B 买{fmt_yield(ws.b_buy, 3)}/卖{fmt_yield(ws.b_sell, 3)}  "
            f"C 买{fmt_yield(ws.c_buy, 3)}/卖{fmt_yield(ws.c_sell, 3)}",
        )

    def manual_point(self) -> None:
        last = float(self.current_point) if self.current_point is not None else None
        dlg = _ManualPointDialog(self, last_point=last)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.point.value()
        if v <= 0:
            QMessageBox.warning(self, "无效点位", "当前指数点位必须 > 0。")
            return
        self.manual_point_override = D(v)
        self.current_point = self.manual_point_override
        self.quote_error = None  # 手工输入覆盖网络错误
        self._update_kpi()
        self._update_mech_card()
        self._update_action_box()
        self._update_threshold_table()
        self.statusBar().showMessage(f"已手工设置当前点位 = {v:.2f}", 5000)

    def do_update(self) -> Tuple[int, Optional[str]]:
        """抓取红利查并 UPSERT。失败不影响历史。"""
        try:
            records, err = honglicha.fetch_valuation_records(get_user_agent(self.config))
            if err:
                self.fetch_error = err
                return 0, err
            n = self.db.upsert_many(records)
            self.fetch_error = None
            # 重新刷新 anchor（latest 变了）
            self._update_anchor()
            return n, None
        except Exception as exc:
            self.fetch_error = f"红利查数据抓取失败：{exc}"
            return 0, self.fetch_error
