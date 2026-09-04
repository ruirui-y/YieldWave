"""主窗口：顶栏指标 + 今日动作 + 阈值表 + 各功能标签页。"""

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
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import get_user_agent
from ..database import Database
from ..models import POS_EMPTY, ValuationRecord, WeeklyStrategy
from ..strategy import (
    ACT_BUY,
    ACT_HOLD,
    ACT_SELL,
    ACT_WAIT,
    compute_medians,
    compute_thresholds,
    current_week_id,
    evaluate_position,
    generate_weekly_strategy,
    total_suggested_percent,
)
from ..data_sources import honglicha
from .backtest_widget import BacktestWidget
from .chart_widget import ChartWidget
from .data_widget import DataWidget
from .trade_widget import TradeWidget

SIGNAL_LABEL = {ACT_BUY: "买入", ACT_SELL: "卖出", ACT_HOLD: "继续持有", ACT_WAIT: "空仓等待"}
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
        self.thresholds: Dict[str, float] = {}
        self.signals: Dict[str, Tuple[str, str]] = {}
        self.fetch_error: Optional[str] = None

        self.setWindowTitle("YieldWave · 红利低波股息率波段助手")
        self.resize(1100, 760)
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
        self.trade_w = TradeWidget(self.db, self.config, get_signals=self.get_signals, on_changed=self.refresh_all)
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
        self.update_signals()
        self._update_metrics()
        self._update_action_box()
        self._update_threshold_table()
        # 子组件刷新
        self.chart_w.set_data(self.records, self.thresholds,
                              weekly_m42=(self.weekly.m42 if self.weekly else None))
        self.trade_w.refresh()
        self.data_w.refresh_stats()

    def update_signals(self) -> None:
        m42 = self.medians.get("M42")
        if self.weekly is not None:
            self.thresholds = {
                "A_buy": self.weekly.a_buy, "A_sell": self.weekly.a_sell,
                "B_buy": self.weekly.b_buy, "B_sell": self.weekly.b_sell,
                "C_buy": self.weekly.c_buy, "C_sell": self.weekly.c_sell,
            }
        elif m42 is not None:
            self.thresholds = compute_thresholds(m42, self.config)
        else:
            self.thresholds = {}

        cur = self.latest.dividend_yield_2 if self.latest else None
        self.signals = {}
        for p in self.positions:
            if cur is None or not self.thresholds:
                self.signals[p.name] = (ACT_WAIT, "暂无有效股息率数据")
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
    def _fmt(self, v: Optional[float]) -> str:
        return f"{v:.2f}" if isinstance(v, (int, float)) else "-"

    def _update_metrics(self) -> None:
        n = len(self.records)
        warm = ""
        if n < self.config["primary_window"]:
            warm = f"  ⚠️ 数据热身中：当前 {n} / {self.config['primary_window']} 个交易日"
        latest_date = self.latest.date.isoformat() if self.latest else "-"
        dy2 = self._fmt(self.latest.dividend_yield_2 if self.latest else None)
        weekly_m42 = self._fmt(self.weekly.m42) if self.weekly else "未锁定（本周策略尚未锁定）"
        status = ""
        if self.fetch_error:
            status = f"\n⚠️ {self.fetch_error}"
        self.metrics.setText(
            f"最新数据日期：{latest_date}    最新股息率2 (D/P2)：{dy2}%\n"
            f"M20：{self._fmt(self.medians.get('M20'))}%    "
            f"M42：{self._fmt(self.medians.get('M42'))}%    "
            f"M60：{self._fmt(self.medians.get('M60'))}%\n"
            f"本周锁定 M42：{weekly_m42}{warm}{status}"
        )

    def _update_action_box(self) -> None:
        # 清空旧控件
        while self.action_layout.count():
            item = self.action_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        cur = self.latest.dividend_yield_2 if self.latest else None
        for p in self.positions:
            act, reason = self.signals.get(p.name, (ACT_WAIT, ""))
            line = QLabel(
                f"{p.label} {p.percent:.0f}%：{SIGNAL_LABEL[act]}    "
                f"（{reason}）"
            )
            line.setStyleSheet(
                f"color:{SIGNAL_COLOR[act]}; font-size:16px; font-weight:bold; padding:2px;"
            )
            self.action_layout.addWidget(line)
        total = total_suggested_percent(self.positions, self.config["core_percent"])
        core = self.config["core_percent"]
        held = [p.label for p in self.positions if p.status == POS_EMPTY]
        summary = QLabel(
            f"总建议仓位：核心 {core:.0f}%"
            + ("".join(f" + {p.label}{p.percent:.0f}%" for p in self.positions if p.status != POS_EMPTY))
            + f" = {total:.0f}%"
        )
        summary.setStyleSheet("font-size:18px; font-weight:bold; color:#ffb300; padding:4px;")
        self.action_layout.addWidget(summary)
        note = QLabel(
            f"（数据日期：{self.latest.date.isoformat() if self.latest else '-'}；"
            f"程序只产生信号，不自动下单。投资有风险，历史回测不代表未来收益。）"
        )
        note.setStyleSheet("color:#9e9e9e; font-size:11px;")
        self.action_layout.addWidget(note)

    def _update_threshold_table(self) -> None:
        self.thr_table.setRowCount(len(self.positions))
        cur = self.latest.dividend_yield_2 if self.latest else None
        for i, p in enumerate(self.positions):
            act, _ = self.signals.get(p.name, (ACT_WAIT, ""))
            self.thr_table.setItem(i, 0, QTableWidgetItem(f"{p.label} {p.percent:.0f}%"))
            self.thr_table.setItem(i, 1, QTableWidgetItem(p.status))
            self.thr_table.setItem(i, 2, QTableWidgetItem(self._fmt(self.thresholds.get(f"{p.name}_buy"))))
            self.thr_table.setItem(i, 3, QTableWidgetItem(self._fmt(self.thresholds.get(f"{p.name}_sell"))))
            self.thr_table.setItem(i, 4, QTableWidgetItem(self._fmt(cur)))
            act_item = QTableWidgetItem(SIGNAL_LABEL[act])
            # 用文字 + 颜色双重区分
            from PyQt6.QtGui import QColor
            act_item.setForeground(QColor(SIGNAL_COLOR[act]))
            self.thr_table.setItem(i, 5, act_item)

    # ---------------- 动作 ----------------
    def lock_week(self) -> None:
        m42 = self.medians.get("M42")
        if m42 is None:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "无法锁定", "数据不足，无法计算 M42。")
            return
        ws = generate_weekly_strategy(m42, self.config)
        self.db.save_weekly_strategy(ws)
        self.weekly = ws
        self.update_signals()
        self.refresh_all()
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.information(
            self, "已锁定",
            f"本周（{ws.week_id}）策略已锁定：\nM42={ws.m42:.2f}%\n"
            f"A 买{ws.a_buy:.2f}/卖{ws.a_sell:.2f}  "
            f"B 买{ws.b_buy:.2f}/卖{ws.b_sell:.2f}  "
            f"C 买{ws.c_buy:.2f}/卖{ws.c_sell:.2f}",
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
