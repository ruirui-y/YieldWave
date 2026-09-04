"""交易记录页面：确认买卖、仓位状态、成交明细。

程序只产生信号，不自动下单。用户根据信号点击“确认已买入 / 确认已卖出”后才改变仓位状态。
"""

from __future__ import annotations

import datetime as _dt
from typing import Callable, Dict, List, Optional, Tuple

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database import Database
from ..models import POS_EMPTY, POS_HOLDING, Trade, ValuationRecord
from ..strategy import ACT_BUY, ACT_SELL

SIGNAL_NAMES = {"BUY": "买入", "SELL": "卖出", "HOLD": "持有", "WAIT": "等待"}


class _ConfirmDialog(QDialog):
    def __init__(self, action: str, position_label: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"{'确认买入' if action == ACT_BUY else '确认卖出'} · {position_label}")
        fl = QFormLayout(self)
        self.price = QDoubleSpinBox()
        self.price.setRange(0, 100000)
        self.price.setDecimals(3)
        self.amount = QDoubleSpinBox()
        self.amount.setRange(0, 1e12)
        self.amount.setDecimals(2)
        self.note = QLineEdit()
        fl.addRow("ETF 实际成交价:", self.price)
        fl.addRow("成交金额 (元):", self.amount)
        fl.addRow("备注:", self.note)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        fl.addRow(btns)


class TradeWidget(QWidget):
    def __init__(
        self,
        db: Database,
        config: Dict,
        get_signals: Callable[[], Dict[str, Tuple[str, str]]],
        on_changed: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.db = db
        self.config = config
        self.get_signals = get_signals
        self.on_changed = on_changed
        self._build_ui()

    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.addWidget(QLabel("仓位状态（程序只给信号，需你手动确认成交）："))

        self.pos_table = QTableWidget()
        self.pos_table.setColumnCount(9)
        self.pos_table.setHorizontalHeaderLabels(
            ["仓位", "状态", "买入日期", "买入股息率", "买入价", "卖出日期", "卖出股息率", "卖出价", "操作"]
        )
        self.pos_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.pos_table)

        v.addWidget(QLabel("成交记录："))
        self.trade_table = QTableWidget()
        self.trade_table.setColumnCount(8)
        self.trade_table.setHorizontalHeaderLabels(
            ["ID", "仓位", "动作", "信号日期", "执行日期", "股息率", "成交价", "金额/备注"]
        )
        self.trade_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.trade_table)
        self.refresh()

    def refresh(self) -> None:
        positions = self.db.get_positions()
        signals = self.get_signals()
        self.pos_table.setRowCount(len(positions))
        for i, p in enumerate(positions):
            act, _ = signals.get(p.name, ("WAIT", ""))
            self.pos_table.setItem(i, 0, QTableWidgetItem(f"{p.label} {p.percent:.0f}%"))
            self.pos_table.setItem(i, 1, QTableWidgetItem(p.status))
            self.pos_table.setItem(i, 2, QTableWidgetItem(p.buy_date or "-"))
            self.pos_table.setItem(i, 3, QTableWidgetItem(f"{p.buy_yield:.2f}" if p.buy_yield else "-"))
            self.pos_table.setItem(i, 4, QTableWidgetItem(f"{p.buy_price:.3f}" if p.buy_price else "-"))
            self.pos_table.setItem(i, 5, QTableWidgetItem(p.sell_date or "-"))
            self.pos_table.setItem(i, 6, QTableWidgetItem(f"{p.sell_yield:.2f}" if p.sell_yield else "-"))
            self.pos_table.setItem(i, 7, QTableWidgetItem(f"{p.sell_price:.3f}" if p.sell_price else "-"))

            btn = QPushButton()
            if act == ACT_BUY and p.status == POS_EMPTY:
                btn.setText("确认已买入")
                btn.clicked.connect(lambda _=False, n=p.name: self._confirm_buy(n))
            elif act == ACT_SELL and p.status == POS_HOLDING:
                btn.setText("确认已卖出")
                btn.clicked.connect(lambda _=False, n=p.name: self._confirm_sell(n))
            elif p.status == POS_HOLDING:
                btn.setText("持有中(不动)")
                btn.setEnabled(False)
            else:
                btn.setText("空仓(不动)")
                btn.setEnabled(False)
            self.pos_table.setCellWidget(i, 8, btn)

        trades = self.db.get_trades()
        self.trade_table.setRowCount(min(len(trades), 500))
        for i, t in enumerate(trades[:500]):
            self.trade_table.setItem(i, 0, QTableWidgetItem(str(t.id)))
            self.trade_table.setItem(i, 1, QTableWidgetItem(t.position_name))
            self.trade_table.setItem(i, 2, QTableWidgetItem(SIGNAL_NAMES.get(t.action, t.action)))
            self.trade_table.setItem(i, 3, QTableWidgetItem(t.signal_date))
            self.trade_table.setItem(i, 4, QTableWidgetItem(t.execution_date))
            self.trade_table.setItem(i, 5, QTableWidgetItem(f"{t.dividend_yield:.2f}" if t.dividend_yield else "-"))
            self.trade_table.setItem(i, 6, QTableWidgetItem(f"{t.etf_price:.3f}" if t.etf_price else "-"))
            extra = f"{t.amount:.2f}" if t.amount else "-"
            if t.note:
                extra += f" / {t.note}"
            self.trade_table.setItem(i, 7, QTableWidgetItem(extra))

    def _confirm_buy(self, name: str) -> None:
        dlg = _ConfirmDialog(ACT_BUY, name, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        signals = self.get_signals()
        act, _ = signals.get(name, ("WAIT", ""))
        p = self.db.get_position(name)
        if p is None:
            return
        p.status = POS_HOLDING
        p.buy_date = _dt.date.today().isoformat()
        p.buy_yield = self._current_yield()
        p.sell_date = None
        p.sell_yield = None
        p.sell_price = None
        self.db.save_position(p)
        price = dlg.price.value()
        amount = dlg.amount.value()
        shares = (amount / price) if price > 0 else 0.0
        self.db.add_trade(
            Trade(
                id=None, position_name=name, action=ACT_BUY,
                signal_date=p.buy_date, execution_date=p.buy_date,
                dividend_yield=p.buy_yield, m42=self._current_m42(),
                threshold=self._current_thresholds().get(f"{name}_buy"),
                percentage=p.percent, etf_price=price if price > 0 else None,
                shares=shares if price > 0 else None,
                amount=amount if amount > 0 else None, note=dlg.note.text() or None,
            )
        )
        self.refresh()
        if self.on_changed:
            self.on_changed()

    def _confirm_sell(self, name: str) -> None:
        dlg = _ConfirmDialog(ACT_SELL, name, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        p = self.db.get_position(name)
        if p is None:
            return
        p.status = POS_EMPTY
        p.sell_date = _dt.date.today().isoformat()
        p.sell_yield = self._current_yield()
        self.db.save_position(p)
        price = dlg.price.value()
        amount = dlg.amount.value()
        shares = (amount / price) if price > 0 else 0.0
        self.db.add_trade(
            Trade(
                id=None, position_name=name, action=ACT_SELL,
                signal_date=p.sell_date, execution_date=p.sell_date,
                dividend_yield=p.sell_yield, m42=self._current_m42(),
                threshold=self._current_thresholds().get(f"{name}_sell"),
                percentage=p.percent, etf_price=price if price > 0 else None,
                shares=shares if price > 0 else None,
                amount=amount if amount > 0 else None, note=dlg.note.text() or None,
            )
        )
        self.refresh()
        if self.on_changed:
            self.on_changed()

    # 由 main_window 注入的上下文
    def _current_yield(self) -> Optional[float]:
        rec = self.db.get_latest()
        return rec.dividend_yield_2 if rec else None

    def _current_m42(self) -> Optional[float]:
        recs = self.db.get_all_valuations()
        if len(recs) < 42:
            return None
        return self._rolling_m42(recs, 42)

    @staticmethod
    def _rolling_m42(recs, n):
        from ..strategy import rolling_median
        vals = [r.dividend_yield_2 for r in recs if r.dividend_yield_2 is not None]
        return rolling_median(vals, n)

    def _current_thresholds(self) -> Dict[str, float]:
        from ..strategy import compute_thresholds
        m42 = self._current_m42()
        if m42 is None:
            return {}
        return compute_thresholds(m42, self.config)
