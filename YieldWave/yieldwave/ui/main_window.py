"""主窗口：按固定规格重排 YieldWave 主窗口。"""

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
    QSplitter,
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
    assemble_estimated_tail,
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
        # 官方 D/P2 滞后期间的估算尾巴（运行时数据，不写库、不进策略、不进 records）
        self.estimated_dp2_tail: List[dict] = []
        # 盘中估算状态（独立于正式信号）
        self.anchor: Optional[Anchor] = None
        self.current_point: Optional[Decimal] = None  # 当前指数点位（Decimal 或 None）
        self.current_quote: Optional[market_quote.CurrentQuote] = None
        self.quote_error: Optional[str] = None
        self.manual_point_override: Optional[Decimal] = None  # 手工输入的当前点位

        self.setWindowTitle("YieldWave · 中证红利低波 H30269 · 盘中机械交易助手")
        self.setMinimumSize(1280, 800)
        self.showMaximized()
        self._build_ui()
        self._build_refresh_timer()
        self.refresh_all()
        # 启动后异步刷新一次行情（不阻塞 UI）
        QTimer.singleShot(1500, self.refresh_current_point_async)

    # ---------------- UI 构建 ----------------
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 8)
        root.setSpacing(10)
        self.setCentralWidget(central)

        # 顶行：标题 + 操作按钮（固定）
        root.addLayout(self._build_header_row(), 0)
        # Hero banner：下一动作（按动作着色左边框，固定）
        root.addWidget(self._build_hero_banner(), 0)
        # KPI 条：官方锚点 + 当前点位 + 估算 D/P2 + 中位数（固定，2 行紧凑）
        root.addWidget(self._build_kpi_strip(), 0)

        # 主区：Tab 拿走所有剩余空间（走势图不再被挤压）
        self.tabs = QTabWidget()
        self.chart_w = ChartWidget()

        # 隐藏图表控件中的下拉框和特定标签
        for _combo in self.chart_w.findChildren(QComboBox):
            _combo.hide()
        for _label in self.chart_w.findChildren(QLabel):
            if _label.text() == "时间范围：" or "鼠标移入走势图自动吸附最近交易日" in _label.text():
                _label.hide()

        self.backtest_w = BacktestWidget(self.db, self.config, on_config_changed=self.reload_config)
        self.trade_w = TradeWidget(
            self.db, self.config,
            get_signals=self.get_signals,
            get_weekly=lambda: self.weekly,
            on_changed=self.refresh_all,
        )
        self.data_w = DataWidget(self.db, self.config, on_update=self.do_update, on_changed=self.refresh_all)
        self.tabs.addTab(self._build_trade_view(), "今日操作")
        self.tabs.addTab(self.backtest_w, "回测/优化")
        self.tabs.addTab(self.trade_w, "交易记录")
        self.tabs.addTab(self.data_w, "数据管理")
        root.addWidget(self.tabs, 1)

        # 免责声明常驻状态栏（不再占卡片空间）
        self._init_disclaimer_statusbar()

    def _build_header_row(self) -> QHBoxLayout:
        hdr = QHBoxLayout()
        title = QLabel("YieldWave · 中证红利低波 H30269")
        title.setObjectName("AppTitle")
        hdr.addWidget(title)
        hdr.addStretch(1)
        self.refresh_btn = QPushButton("刷新当前点位")
        self.refresh_btn.setObjectName("PrimaryButton")
        self.refresh_btn.setFixedSize(132, 32)
        self.refresh_btn.clicked.connect(self.refresh_current_point_async)
        hdr.addWidget(self.refresh_btn)
        self.manual_btn = QPushButton("手工输入点位")
        self.manual_btn.setFixedSize(132, 32)
        self.manual_btn.clicked.connect(self.manual_point)
        hdr.addWidget(self.manual_btn)
        self.lock_btn = QPushButton("锁定本周策略")
        self.lock_btn.setFixedSize(132, 32)
        self.lock_btn.clicked.connect(self.lock_week)
        hdr.addWidget(self.lock_btn)
        return hdr

    def _build_hero_banner(self) -> QFrame:
        self.hero_banner = QFrame()
        self.hero_banner.setObjectName("HeroBanner")
        self.hero_banner.setProperty("action", "wait")
        self.hero_banner.setFixedHeight(68)
        hb = QVBoxLayout(self.hero_banner)
        hb.setContentsMargins(16, 10, 16, 10)
        hb.setSpacing(2)
        self.hero_title = QLabel("下一动作：--")
        self.hero_title.setObjectName("HeroTitle")
        self.hero_sub = QLabel("--")
        self.hero_sub.setObjectName("HeroSub")
        hb.addWidget(self.hero_title)
        hb.addWidget(self.hero_sub)
        return self.hero_banner

    def _build_kpi_strip(self) -> QFrame:
        self.kpi_frame = QFrame()
        self.kpi_frame.setObjectName("KpiStrip")
        self.kpi_frame.setFixedHeight(78)
        kv = QHBoxLayout(self.kpi_frame)
        kv.setContentsMargins(0, 0, 0, 0)
        kv.setSpacing(0)
        self.lbl_anchor_dp2, self.sub_anchor_dp2 = self._kpi_cell(kv, "官方 D/P2")
        self._kpi_vline(kv)
        self.lbl_est_dp2, self.sub_est_dp2 = self._kpi_cell(kv, "估算当前 D/P2")
        self.lbl_est_dp2.setProperty("est", True)
        self._kpi_vline(kv)
        self.lbl_current_point, self.sub_current_point = self._kpi_cell(kv, "当前点位")
        self._kpi_vline(kv)
        self.lbl_anchor_close, self.sub_anchor_close = self._kpi_cell(kv, "锚点收盘")
        self._kpi_vline(kv)
        self.lbl_m42, self.sub_m42 = self._kpi_cell(kv, "M42 中位数")
        return self.kpi_frame

    def _build_trade_view(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_chart_pane())
        splitter.addWidget(self._build_side_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([900, 480])
        layout.addWidget(splitter, 1)
        return tab

    def _build_chart_pane(self) -> QWidget:
        pane = QWidget()
        pv = QVBoxLayout(pane)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(6)
        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(8)
        tools.setSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)
        label = QLabel("时间范围")
        label.setObjectName("MutedNote")
        tools.addWidget(label)
        self.range_combo = QComboBox()
        self.range_combo.addItems(["1个月", "2个月", "3个月", "6个月", "1年"])
        self.range_combo.setCurrentText("6个月")  # 与 chart 内部默认范围对齐，避免初始不一致
        self.range_combo.setFixedSize(110, 26)
        tools.addWidget(self.range_combo)
        self.range_combo.currentTextChanged.connect(self.chart_w.set_range)
        tools.addStretch(1)
        hover = QLabel("鼠标移入走势图自动吸附最近交易日")
        hover.setObjectName("MutedNote")
        tools.addWidget(hover)
        pv.addLayout(tools)
        self.chart_w.setMinimumSize(720, 460)
        pv.addWidget(self.chart_w, 1)
        return pane

    def _build_side_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(460)
        panel.setMaximumWidth(500)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(10)
        pv.addWidget(self._build_side_card_a())
        pv.addWidget(self._build_side_card_b())
        pv.addWidget(self._build_side_card_c())
        pv.addStretch(1)
        return panel

    def _build_side_card(self, title: str, fixed_height: int) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("SideCard")
        card.setFixedHeight(fixed_height)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        title_label = QLabel(title)
        title_label.setObjectName("SideCardTitle")
        lay.addWidget(title_label)
        return card, lay

    def _build_side_card_a(self) -> QFrame:
        card, lay = self._build_side_card("A/B/C 机械点位", 224)
        self.side_table = QTableWidget()
        self.side_table.setObjectName("ThresholdTable")
        self.side_table.setColumnCount(5)
        self.side_table.setRowCount(3)
        self.side_table.setHorizontalHeaderLabels(["仓位", "买入线", "卖出线", "距离", "动作"])
        self.side_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.side_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.side_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.side_table.verticalHeader().setVisible(False)
        self.side_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.side_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.side_table.setShowGrid(False)
        self.side_table.horizontalHeader().setFixedHeight(30)
        self.side_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        for col, width in enumerate([60, 116, 116, 84, 64]):
            self.side_table.setColumnWidth(col, width)
        for row in range(3):
            self.side_table.setRowHeight(row, 46)
        self.side_table.setFixedHeight(170)
        lay.addWidget(self.side_table)
        return card

    def _build_side_card_b(self) -> QFrame:
        card, lay = self._build_side_card("锚点与数据", 124)
        self.anchor_block = self._side_note(lay)
        self.data_source_block = self._side_note(lay)
        self.valid_data_block = self._side_note(lay)
        self.position_block = self._side_note(lay)
        return card

    def _build_side_card_c(self) -> QFrame:
        card, lay = self._build_side_card("今日正式信号", 96)
        self.signal_block = QLabel("等待锁定本周策略（未锁定不产生正式 BUY/SELL）")
        self.signal_block.setObjectName("WarnNote")
        self.signal_block.setWordWrap(True)
        self.signal_sub_block = QLabel("锁定后基于官方已公布 D/P2 触发")
        self.signal_sub_block.setObjectName("MutedNote")
        self.signal_sub_block.setWordWrap(True)
        lay.addWidget(self.signal_block)
        lay.addWidget(self.signal_sub_block)
        return card

    def _side_note(self, layout: QVBoxLayout) -> QLabel:
        label = QLabel("--")
        label.setObjectName("MutedNote")
        label.setWordWrap(True)
        layout.addWidget(label)
        return label

    def _kpi_vline(self, layout: QHBoxLayout) -> None:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        layout.addWidget(line)

    def _kpi_cell(self, layout: QHBoxLayout, label_text: str) -> tuple[QLabel, QLabel]:
        cell = QVBoxLayout()
        cell.setContentsMargins(16, 12, 16, 12)
        cell.setSpacing(2)
        lbl = QLabel(label_text)
        lbl.setObjectName("KpiLabel")
        val = QLabel("--")
        val.setObjectName("KpiValue")
        sub = QLabel("--")
        sub.setObjectName("KpiSub")
        cell.addWidget(lbl)
        cell.addWidget(val)
        cell.addWidget(sub)
        wrapper = QWidget()
        wrapper.setLayout(cell)
        layout.addWidget(wrapper)
        return val, sub
    def _init_disclaimer_statusbar(self) -> None:
        self.disclaimer_label = QLabel(
            "核心仓 build_percentile=50/65/80 为待回测确认初值；程序只产生信号，不自动下单。"
            "投资有风险，历史回测不代表未来收益。"
        )
        self.disclaimer_label.setObjectName("MutedNote")
        self.statusBar().addPermanentWidget(self.disclaimer_label)

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
        self._update_hero()
        self._update_side_table()
        # 子组件刷新
        self._refresh_chart()
        self.trade_w.refresh()
        self.data_w.refresh_stats()

    def _refresh_chart(self) -> None:
        """构建官方 D/P2 滞后期间的估算尾巴并刷新走势图。

        估算尾巴只用于把红利查滞后的交易日补到图上观察真实拐点：
        - 不写 h30269_valuation 数据库；
        - 不并入 self._records（官方与估算始终分开）；
        - 不参与 M42 / Mean42 / A/B/C 策略。
        """
        self.estimated_dp2_tail = self._build_estimated_tail()
        self.chart_w.set_data(
            self.records,
            self.thresholds if self.locked else self.preview_thresholds,
            weekly_m42=(self.weekly.m42 if self.weekly else None),
            estimated_dp2_tail=self.estimated_dp2_tail,
        )

    def _persist_daily_estimated_dp2(self, trade_date: Optional[str]) -> None:
        """把当天成功取得的行情估算 D/P2 持久化到独立表。

        只保存：
        - 有真实 trade_date（即交易日）
        - anchor 有效（官方 D/P2 锚点 + 收盘）
        - 当前指数点位有效
        同一天重复调用由数据库按 trade_date UPSERT，每天只留最新一条。
        """
        if not trade_date:
            return
        if self.anchor is None or not self.anchor.valid():
            return
        if self.current_point is None or self.current_point <= 0:
            return
        est = estimate_current_dp2(
            self.anchor.dp2,
            self.anchor.close,
            self.current_point,
        )
        if est is None:
            return
        self.db.upsert_estimated_dp2(
            trade_date=trade_date,
            estimated_dp2=est,
            index_point=self.current_point,
            anchor_date=self.anchor.date,
            anchor_dp2=self.anchor.dp2,
            anchor_close=self.anchor.close,
        )

    def _build_estimated_tail(self) -> List[dict]:
        """生成本地运行时的估算 D/P2 尾巴（anchor 之后、未公布官方 D/P2 的交易日）。

        公式固定为 anchor_dp2 * anchor_close / 当日点位，所有点都【直接相对最后一个官方锚点】
        计算，绝不拿前一天估算值递归（无链式误差）。周末/非交易日不人为生成数据点。

        返回结构固定：[{date, dp2(Decimal), index_point(Decimal), kind}, ...]
        kind="close"  用已结束交易日官方收盘反推
        kind="intraday"  当天用实时 current 反推
        anchor 无效 / 无当前点位时返回空列表。
        """
        if self.anchor is None or not self.anchor.valid():
            return []
        if self.current_point is None or self.current_point <= 0:
            return []
        anchor_date = self.anchor.date
        anchor_d = _dt.date.fromisoformat(anchor_date)
        # 当天（盘中）交易日：优先用实时行情 trade_date，否则回退到今天
        intraday_date = None
        if self.current_quote is not None and self.current_quote.trade_date:
            intraday_date = self.current_quote.trade_date
        else:
            intraday_date = _dt.date.today().isoformat()
        # 拉取 anchor 之后到当天的每日收盘（已结束交易日，不含 anchor 当天）
        start = (anchor_d + _dt.timedelta(days=1)).isoformat()
        daily_closes, _err = market_quote.fetch_close_range(start, intraday_date)
        # 网络失败 / 当日无数据 -> 不崩，返回空尾巴（UI 照常显示官方曲线）
        if not daily_closes:
            return []
        return assemble_estimated_tail(
            anchor_date=anchor_date,
            anchor_dp2=self.anchor.dp2,
            anchor_close=self.anchor.close,
            daily_closes=daily_closes,
            current_date=intraday_date,
            current_point=self.current_point,
        )

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
            # 如果当前行情点位已经在手，只是此前因缺 anchor_close 无法保存，
            # 现在补存当天估算 D/P2。
            if self.current_quote is not None:
                self._persist_daily_estimated_dp2(self.current_quote.trade_date)
            self._update_kpi()
            self._update_hero()
            self._update_side_table()
            self._refresh_chart()
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
            self.current_quote = quote
            self.quote_error = None
            # 手工 override 仍优先
            if self.manual_point_override is None:
                self.current_point = D(quote.current)
                # 成功取得当天行情并算出估算 D/P2 后，持久化一天一条
                self._persist_daily_estimated_dp2(quote.trade_date)
        else:
            self.quote_error = err or "行情抓取失败，使用最后一次成功数据"
        self._update_kpi()
        self._update_hero()
        self._update_side_table()
        # 行情刷新成功且 anchor 有效 -> 重建估算尾巴并刷新走势图
        self._refresh_chart()

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
        total = len(self.records)
        official_date = self.latest.date.isoformat() if self.latest is not None else "--"
        self.lbl_anchor_dp2.setText(
            f"{self._fmt(self.latest.dividend_yield_2, 2)}%"
            if self.latest is not None and self.latest.dividend_yield_2 is not None else "--"
        )
        self.sub_anchor_dp2.setText(f"官方日期 {official_date}")

        est_dp2 = estimate_current_dp2(
            self.anchor.dp2 if self.anchor else None,
            self.anchor.close if self.anchor else None,
            self.current_point,
        )
        self.lbl_est_dp2.setText(f"{self._fmt(est_dp2, 3)}%" if est_dp2 is not None else "--")
        self.lbl_est_dp2.setProperty("est", True)
        self.lbl_est_dp2.style().unpolish(self.lbl_est_dp2)
        self.lbl_est_dp2.style().polish(self.lbl_est_dp2)
        diff = None
        if est_dp2 is not None and self.latest is not None and self.latest.dividend_yield_2 is not None:
            diff = est_dp2 - self.latest.dividend_yield_2
        self.sub_est_dp2.setText(f"较官方 {_fmt_dist(diff, 3)}pp" if diff is not None else "较官方 --pp")

        self.lbl_current_point.setText(_fmt_point(self.current_point) if self.current_point is not None else "--")
        update_time = "--"
        if self.current_quote is not None and self.current_quote.trade_time:
            update_time = self.current_quote.trade_time
        self.sub_current_point.setText(f"更新 {update_time}")

        self.lbl_anchor_close.setText(
            _fmt_point(self.anchor.close) if self.anchor is not None and self.anchor.close is not None else "--"
        )
        anchor_date = self.anchor.date if self.anchor is not None else "--"
        self.sub_anchor_close.setText(f"锚定日 {anchor_date}")

        self.lbl_m42.setText(f"{self._fmt(self.medians.get('M42'))}%")
        self.sub_m42.setText(f"M20 {self._fmt(self.medians.get('M20'))} · M60 {self._fmt(self.medians.get('M60'))}")

    def _set_hero_action(self, action: str) -> None:
        self.hero_banner.setProperty("action", action)
        self.hero_banner.style().unpolish(self.hero_banner)
        self.hero_banner.style().polish(self.hero_banner)

    def _hero_sub_hint(self) -> str:
        suffix = " · [预览·本周未锁定]" if not self.locked else ""
        if self.anchor is None or not self.anchor.valid():
            return f"当前点位 -- · 目标 -- · 距离 -- 点（--%）{suffix}"
        if self.current_point is None or self.current_point <= 0:
            return f"当前点位 -- · 目标 -- · 距离 -- 点（--%）{suffix}"
        return f"当前点位 {_fmt_point(self.current_point)} · 目标 -- · 距离 -- 点（--%）{suffix}"

    def _update_hero(self) -> None:
        nxt = self._next_mech_action()
        if nxt is None:
            self.hero_title.setText("下一动作：等待")
            self.hero_sub.setText(self._hero_sub_hint())
            self._set_hero_action("wait")
            return
        p, action, target_pt, dist_pt, dist_pct = nxt
        verb = "买入" if action == "buy" else "卖出"
        self.hero_title.setText(f"下一动作：{p.label}{verb} {p.percent:.0f}% · 目标点位 {_fmt_point(target_pt)}")
        suffix = "[预览·本周未锁定]" if not self.locked else "[本周已锁定]"
        self.hero_sub.setText(
            f"当前点位 {_fmt_point(self.current_point)} · 目标 {_fmt_point(target_pt)} · "
            f"距离 {_fmt_dist(dist_pt)} 点（{_fmt_dist(dist_pct)}%）· {suffix}"
        )
        self._set_hero_action(action)

    def _next_mech_action(self):
        if self.anchor is None or not self.anchor.valid():
            return None
        if self.current_point is None or self.current_point <= 0:
            return None
        use = self.thresholds if self.locked else self.preview_thresholds
        if not use:
            return None
        candidates = []
        for p in self.positions:
            if p.kind == "core":
                continue
            key = f"{p.name}_buy" if p.status == POS_EMPTY else f"{p.name}_sell"
            action = "buy" if p.status == POS_EMPTY else "sell"
            target_dp2 = use.get(key)
            target_pt = yield_to_target_point(
                self.anchor.dp2,
                self.anchor.close,
                target_dp2,
            ) if target_dp2 is not None else None
            if target_pt is None or target_pt <= 0:
                continue
            dist = distance_to_target(self.current_point, target_pt)
            if dist:
                candidates.append((p, action, target_pt, dist[0], dist[1]))
        return min(candidates, key=lambda c: abs(c[3])) if candidates else None

    def _update_side_table(self) -> None:
        use = self.thresholds if self.locked else self.preview_thresholds
        swing = [p for p in self.positions if p.kind != "core"]
        swing = sorted(swing, key=lambda p: self.config["positions"][p.name].get("percent", 0))
        self.side_table.setRowCount(len(swing))
        for i, p in enumerate(swing):
            buy_dp2 = use.get(f"{p.name}_buy")
            sell_dp2 = use.get(f"{p.name}_sell")
            buy_pt = yield_to_target_point(
                self.anchor.dp2 if self.anchor else None,
                self.anchor.close if self.anchor else None,
                buy_dp2,
            ) if buy_dp2 is not None else None
            sell_pt = yield_to_target_point(
                self.anchor.dp2 if self.anchor else None,
                self.anchor.close if self.anchor else None,
                sell_dp2,
            ) if sell_dp2 is not None else None
            dist_text = "--"
            action_text = "等待买入" if p.status == POS_EMPTY else "持有"
            action_color = theme.TEXT_MUTED
            if p.status == POS_EMPTY and buy_pt is not None:
                dist = distance_to_target(self.current_point, buy_pt)
                if dist:
                    dist_text = f"{_fmt_dist(dist[0])} 点"
                    if dist[0] <= 0:
                        action_text = "可执行"
                        action_color = theme.BUY
            elif p.status == POS_HOLDING and sell_pt is not None:
                dist = distance_to_target(self.current_point, sell_pt)
                if dist:
                    dist_text = f"{_fmt_dist(dist[0])} 点"
                    if dist[0] >= 0:
                        action_text = "可执行"
                        action_color = theme.SELL
            if not self.locked:
                action_text = f"{action_text}(预览)"

            values = [
                f"{p.label} {p.percent:.0f}%",
                f"{self._fmt(buy_dp2, 3)}%\n{_fmt_point(buy_pt)}",
                f"{self._fmt(sell_dp2, 3)}%\n{_fmt_point(sell_pt)}",
                dist_text,
                action_text,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 4:
                    item.setForeground(QColor(action_color))
                self.side_table.setItem(i, col, item)
            self.side_table.setRowHeight(i, 46)

        if self.anchor is not None and self.anchor.valid():
            self.anchor_block.setText(
                f"锚点：{self.anchor.date} · 官方 D/P2 {self._fmt(self.anchor.dp2, 2)}% · 收盘 {_fmt_point(self.anchor.close)}"
            )
        else:
            self.anchor_block.setText("锚点：-- · 官方 D/P2 -- · 收盘 --")
        self.data_source_block.setText("数据源：红利查 + 中证官方")
        self.valid_data_block.setText(f"有效数据：{valid_dp2_count(self.records)} / {len(self.records)} 条")
        core = current_core_percent(self.positions)
        sw = current_swing_percent(self.positions)
        self.position_block.setText(f"核心仓：{core:.0f}%（已建）· 波段仓：{sw:.0f}%（已持有）")
        self.signal_block.setText("等待锁定本周策略（未锁定不产生正式 BUY/SELL）")
        self.signal_sub_block.setText("锁定后基于官方已公布 D/P2 触发")
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
        self._update_hero()
        self._update_side_table()
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







