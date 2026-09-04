"""主窗口：顶栏指标 + 今日动作 + 阈值表 + 各功能标签页。

两种周策略状态：
- 未锁定（PREVIEW）：只显示“预览买卖线”，**不产生正式 BUY/SELL 信号**，禁止确认成交。
- 已锁定（LOCKED）：使用本周锁定的 A/B/C 阈值产生正式 BUY/SELL/HOLD/WAIT。
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from decimal import Decimal

from ..config import get_user_agent
from ..database import Database
from ..models import POS_EMPTY, ValuationRecord, WeeklyStrategy
from ..precision import D, fmt_yield
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
    total_suggested_percent,
    valid_dp2_count,
)
from ..data_sources import honglicha
from .backtest_widget import BacktestWidget
from .chart_widget import ChartWidget
from .data_widget import DataWidget
from .trade_widget import TradeWidget

SIGNAL_LABEL = {
    ACT_BUY: "买入", ACT_SELL: "卖出", ACT_HOLD: "继续持有", ACT_WAIT: "空仓等待",
}
SIGNAL_COLOR = {
    ACT_BUY: "#1b7a34",   # 绿
    ACT_SELL: "#c62828",  # 红
    ACT_HOLD: "#1565c0",  # 蓝
    ACT_WAIT: "#757575",  # 灰
}


class MainWindow(QMainWindow):
    def __init__(self, db: Database, config: Dict):
        super().__init__()
        self.db = db
        self.config = config
        self.records: List[ValuationRecord] = []
        self.medians: Dict[str, Optional[float]] = {}
        self.latest: Optional[ValuationRecord] = None
        self.positions: List = []
        self.weekly: Optional[WeeklyStrategy] = None
        self.locked: bool = False
        self.thresholds: Dict[str, float] = {}
        self.preview_thresholds: Dict[str, float] = {}
        self.signals: Dict[str, Tuple[str, str]] = {}
        self.fetch_error: Optional[str] = None

        self.setWindowTitle("YieldWave · 红利低波股息率波段助手")
        self.resize(1180, 800)
        self._build_ui()
        self.refresh_all()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        root = QVBoxLayout()
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        # 顶栏
        hdr = QHBoxLayout()
        title = QLabel("中证红利低波 H30269")
        title.setStyleSheet("font-size:20px; font-weight:bold;")
        hdr.addWidget(title)
        hdr.addStretch(1)
        self.lock_btn = QPushButton("锁定本周策略")
        self.lock_btn.clicked.connect(self.lock_week)
        hdr.addWidget(self.lock_btn)
        root.addLayout(hdr)

        # 指标区
        self.metrics = QLabel("指标加载中…")
        self.metrics.setWordWrap(True)
        self.metrics.setStyleSheet("background:#1e1e1e; padding:8px; border-radius:6px;")
        root.addWidget(self.metrics)

        # 今日动作
        self.action_box = QGroupBox("今日操作")
        self.action_layout = QVBoxLayout(self.action_box)
        root.addWidget(self.action_box)

        # 阈值表
        self.thr_table = QTableWidget()
        self.thr_table.setColumnCount(6)
        self.thr_table.setHorizontalHeaderLabels(
            ["仓位", "当前状态", "买入线", "卖出线", "当前股息率", "动作"]
        )
        self.thr_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        root.addWidget(self.thr_table)

        # 标签页
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
        root.addWidget(tabs)

    # ---------------- 数据流程 ----------------
    def refresh_all(self) -> None:
        self.records = self.db.get_all_valuations()
        self.medians = compute_medians(self.records, self.config["windows"])
        self.latest = self.db.get_latest()
        self.positions = self.db.get_positions()
        self.weekly = self.db.get_weekly_strategy(current_week_id())
        self.locked = self.weekly is not None
        self.update_signals()
        self._update_metrics()
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
                # 核心仓无自动信号
                self.signals[p.name] = (ACT_WAIT, "核心仓：仅手动确认建仓，无自动信号")
                continue
            if cur is None or not self.thresholds:
                self.signals[p.name] = (ACT_WAIT, "本周未锁定，不产生正式信号（仅预览）")
            else:
                act, reason = evaluate_position(p, cur, self.thresholds)
                self.signals[p.name] = (act, reason)

    def current_thresholds(self) -> Dict[str, float]:
        return self.thresholds

    def get_signals(self) -> Dict[str, Tuple[str, str]]:
        return self.signals

    def reload_config(self) -> None:
        from ..config import load_config
        self.config = load_config()
        self.refresh_all()

    # ---------------- 展示 ----------------
    def _fmt(self, v: object, decimals: int = 3) -> str:
        """股息率/阈值统一显示：Decimal 用 ROUND_HALF_UP 量化到 decimals 位；None 显示 '-'。

        中位数与买卖线默认 3 位小数；原始 D/P2（仅 2 位）调用方传 decimals=2。
        """
        if v is None:
            return "-"
        if isinstance(v, Decimal):
            return fmt_yield(v, decimals)
        return fmt_yield(D(v), decimals)

    def _update_metrics(self) -> None:
        valid = valid_dp2_count(self.records)
        n = len(self.records)
        warm = ""
        if valid < self.config["primary_window"]:
            warm = f"  ⚠️ 有效D/P2数据热身中：当前 {valid} / {self.config['primary_window']} 个交易日"
        latest_date = self.latest.date.isoformat() if self.latest else "-"
        dy2 = self._fmt(self.latest.dividend_yield_2 if self.latest else None, 2)
        if self.locked:
            weekly_m42 = self._fmt(self.weekly.m42, 3)
            lock_state = "已锁定"
        else:
            weekly_m42 = "未锁定"
            lock_state = "未锁定（本周尚无正式信号）"
        status = ""
        if self.fetch_error:
            status = f"\n⚠️ {self.fetch_error}"
        self.metrics.setText(
            f"最新数据日期：{latest_date}    最新股息率2 (D/P2)：{dy2}%\n"
            f"M20：{self._fmt(self.medians.get('M20'))}%    "
            f"M42：{self._fmt(self.medians.get('M42'))}%    "
            f"M60：{self._fmt(self.medians.get('M60'))}%\n"
            f"有效D/P2数据：{valid} 条（总记录 {n} 条）\n"
            f"本周策略状态：{lock_state}    本周锁定 M42：{weekly_m42}{warm}{status}"
        )

    def _update_action_box(self) -> None:
        while self.action_layout.count():
            item = self.action_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        cur = self.latest.dividend_yield_2 if self.latest else None

        if not self.locked:
            # 未锁定：只显示“等待锁定”，预览线单独列出，不产生正式信号
            wait = QLabel("今日正式操作：等待锁定本周策略（未锁定前不产生正式 BUY/SELL 信号）")
            wait.setStyleSheet("color:#ffb300; font-size:16px; font-weight:bold; padding:4px;")
            self.action_layout.addWidget(wait)

            prev = QLabel("预览买卖线（非正式信号，锁定后生效）：")
            prev.setStyleSheet("color:#9e9e9e; font-size:12px;")
            self.action_layout.addWidget(prev)
            for name in self.config["positions"]:
                line = QLabel(
                    f"{self.config['positions'][name]['label']} {self.config['positions'][name]['percent']:.0f}%："
                    f"买 {self._fmt(self.preview_thresholds.get(f'{name}_buy'))} / "
                    f"卖 {self._fmt(self.preview_thresholds.get(f'{name}_sell'))}"
                )
                line.setStyleSheet("color:#9e9e9e; font-size:14px; padding:1px;")
                self.action_layout.addWidget(line)
            self._append_summary()
            return

        # 已锁定：正式信号
        for p in self.positions:
            if p.kind == "core":
                continue
            act, reason = self.signals.get(p.name, (ACT_WAIT, ""))
            line = QLabel(
                f"{p.label} {p.percent:.0f}%：{SIGNAL_LABEL[act]}    （{reason}）"
            )
            line.setStyleSheet(
                f"color:{SIGNAL_COLOR[act]}; font-size:16px; font-weight:bold; padding:2px;"
            )
            self.action_layout.addWidget(line)
        self._append_summary()

    def _append_summary(self) -> None:
        core = current_core_percent(self.positions)
        swing = current_swing_percent(self.positions)
        equity = current_equity_percent(self.positions)
        summary = QLabel(
            f"当前实际仓位：核心 {core:.0f}%（已建） + 波段 {swing:.0f}%（已持有） = 实际权益 {equity:.0f}%"
        )
        summary.setStyleSheet("font-size:16px; font-weight:bold; color:#ffb300; padding:4px;")
        self.action_layout.addWidget(summary)
        note = QLabel(
            f"（数据日期：{self.latest.date.isoformat() if self.latest else '-'}；"
            f"核心仓建仓参数 build_percentile=50/65/80 为待回测确认初值，非已验证最优；"
            f"程序只产生信号，不自动下单。投资有风险，历史回测不代表未来收益。）"
        )
        note.setStyleSheet("color:#9e9e9e; font-size:11px;")
        self.action_layout.addWidget(note)

    def _update_threshold_table(self) -> None:
        swing = [p for p in self.positions if p.kind != "core"]
        self.thr_table.setRowCount(len(swing))
        cur = self.latest.dividend_yield_2 if self.latest else None
        for i, p in enumerate(swing):
            act, _ = self.signals.get(p.name, (ACT_WAIT, ""))
            action_text = SIGNAL_LABEL[act] if self.locked else "预览"
            self.thr_table.setItem(i, 0, QTableWidgetItem(f"{p.label} {p.percent:.0f}%"))
            self.thr_table.setItem(i, 1, QTableWidgetItem(p.status))
            self.thr_table.setItem(i, 2, QTableWidgetItem(self._fmt(self.thresholds.get(f"{p.name}_buy") or self.preview_thresholds.get(f"{p.name}_buy"))))
            self.thr_table.setItem(i, 3, QTableWidgetItem(self._fmt(self.thresholds.get(f"{p.name}_sell") or self.preview_thresholds.get(f"{p.name}_sell"))))
            self.thr_table.setItem(i, 4, QTableWidgetItem(self._fmt(cur, 2)))
            act_item = QTableWidgetItem(action_text)
            from PyQt6.QtGui import QColor
            if self.locked:
                act_item.setForeground(QColor(SIGNAL_COLOR[act]))
            else:
                act_item.setForeground(QColor("#9e9e9e"))
            self.thr_table.setItem(i, 5, act_item)

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
        self.db.save_weekly_strategy(ws)  # 默认禁止覆盖
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

    def do_update(self) -> Tuple[int, Optional[str]]:
        """抓取红利查并 UPSERT。返回 (新增/更新条数, error_msg)。失败不影响历史。"""
        try:
            records, err = honglicha.fetch_valuation_records(get_user_agent(self.config))
            if err:
                self.fetch_error = err
                return 0, err
            n = self.db.upsert_many(records)
            self.fetch_error = None
            return n, None
        except Exception as exc:  # 兜底：任何异常都保留旧数据
            self.fetch_error = f"红利查数据抓取失败：{exc}"
            return 0, self.fetch_error
