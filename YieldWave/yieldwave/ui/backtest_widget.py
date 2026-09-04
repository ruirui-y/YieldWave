"""回测与参数优化页面。"""

from __future__ import annotations

import json
import os
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..backtest import run_backtest
from ..config import CONFIG_PATH
from ..database import Database
from ..optimizer import search_parameters


class BacktestWidget(QWidget):
    def __init__(
        self,
        db: Database,
        config: Dict,
        on_config_changed: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.db = db
        self.config = config
        self.on_config_changed = on_config_changed
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_backtest_tab(), "回测")
        tabs.addTab(self._build_optimizer_tab(), "参数优化")
        root.addWidget(tabs)

    # ---------------- 回测 ----------------
    def _build_backtest_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        form = QFormLayout()

        self.window_combo = QComboBox()
        self.window_combo.addItems([str(x) for x in self.config.get("backtest_windows", [20, 30, 42, 50, 60])])
        self.window_combo.setCurrentText("42")
        form.addRow("滚动窗口 (交易日):", self.window_combo)

        self.spins: Dict[str, QDoubleSpinBox] = {}
        for key, label in [
            ("A_buy", "A 买入偏移"), ("A_sell", "A 卖出偏移"),
            ("B_buy", "B 买入偏移"), ("B_sell", "B 卖出偏移"),
            ("C_buy", "C 买入偏移"), ("C_sell", "C 卖出偏移"),
        ]:
            sp = QDoubleSpinBox()
            sp.setRange(-0.5, 0.5)
            sp.setSingleStep(0.01)
            sp.setDecimals(2)
            name = key[0]  # 'A' / 'B' / 'C'
            off_key = "buy_offset" if key.endswith("buy") else "sell_offset"
            sp.setValue(float(self.config["positions"][name][off_key]))
            self.spins[key] = sp
            form.addRow(label + " (百分点):", sp)
        v.addLayout(form)

        btn = QPushButton("运行回测")
        btn.clicked.connect(self.run_backtest)
        v.addWidget(btn)

        self.result_edit = QTextEdit()
        self.result_edit.setReadOnly(True)
        v.addWidget(self.result_edit)
        return w

    # ---------------- 优化 ----------------
    def _build_optimizer_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        note = QLabel(
            "目标：A 仓年均交易次数落入 6~9 次，且 A 仓中位持有 5~20 交易日。"
            "排序优先频率、其次持有时间，绝不只追历史收益最高。"
        )
        note.setWordWrap(True)
        v.addWidget(note)

        btn = QPushButton("开始参数搜索")
        btn.clicked.connect(self.run_search)
        v.addWidget(btn)

        self.opt_table = QTableWidget()
        self.opt_table.setColumnCount(8)
        self.opt_table.setHorizontalHeaderLabels(
            ["窗口", "A买", "A卖", "B买", "B卖", "C买", "C卖", "年均A/中位持有"]
        )
        v.addWidget(self.opt_table)

        apply_btn = QPushButton("应用选中参数到 config.json")
        apply_btn.clicked.connect(self.apply_selected)
        v.addWidget(apply_btn)
        return w

    # ---------------- 逻辑 ----------------
    def _current_offsets(self):
        return {
            "A": (self.spins["A_buy"].value(), self.spins["A_sell"].value()),
            "B": (self.spins["B_buy"].value(), self.spins["B_sell"].value()),
            "C": (self.spins["C_buy"].value(), self.spins["C_sell"].value()),
        }

    def run_backtest(self) -> None:
        records = self.db.get_all_valuations()
        if len(records) < 5:
            self.result_edit.setPlainText("历史数据不足，无法回测。请先更新红利查。")
            return
        window = int(self.window_combo.currentText())
        offsets = self._current_offsets()
        summary = run_backtest(records, window, offsets)
        self.result_edit.setPlainText(self._format_summary(summary))

    @staticmethod
    def _format_summary(s: Dict) -> str:
        lines = []
        lines.append(f"滚动窗口: {s.get('window')} 个交易日")
        lines.append(f"完成总轮数: {s.get('total_rounds')}")
        pc = s.get("per_position_counts", {})
        lines.append(f"各仓交易次数: A={pc.get('A')} B={pc.get('B')} C={pc.get('C')}")
        lines.append(f"平均持有天数: {s.get('avg_holding_days')}")
        lines.append(f"中位持有天数: {s.get('median_holding_days')}")
        lines.append(f"最短/最长持有: {s.get('min_holding_days')} / {s.get('max_holding_days')}")
        lines.append("")
        lines.append("【股息率信号表现（非真实资金）】")
        lines.append(f"  股息率口径胜率: {s.get('win_rate_yield')}")
        lines.append(f"  平均/中位 股息率收益: {s.get('avg_yield_gain')} / {s.get('median_yield_gain')}")
        lines.append(f"  最大单轮亏损/盈利: {s.get('max_yield_loss')} / {s.get('max_yield_gain')}")
        lines.append("")
        lines.append("【真实资金收益（需指数点位/全收益数据）】")
        lines.append(f"  胜率(资金): {s.get('win_rate_price')}")
        lines.append(f"  总收益: {s.get('total_return_price')}")
        lines.append(f"  年化收益: {s.get('annual_return_price')}")
        lines.append(f"  最大回撤: {s.get('max_drawdown_price')}")
        pyr = s.get("per_year_rounds", {})
        if pyr:
            lines.append("")
            lines.append("每年完整波段次数: " + ", ".join(f"{k}:{v}" for k, v in pyr.items()))
        return "\n".join(lines)

    def run_search(self) -> None:
        records = self.db.get_all_valuations()
        if len(records) < 30:
            QMessageBox.warning(self, "数据不足", "历史数据不足 30 个交易日，无法可靠搜索。")
            return
        results = search_parameters(records)
        self._opt_results = results
        self.opt_table.setRowCount(min(len(results), 200))
        for i, r in enumerate(results[:200]):
            mh = r["median_hold_A"]
            mh_s = f"{mh:.0f}" if mh is not None else "-"
            row = [
                str(r["window"]), f"{r['A_buy']:.2f}", f"{r['A_sell']:.2f}",
                f"{r['B_buy']:.2f}", f"{r['B_sell']:.2f}",
                f"{r['C_buy']:.2f}", f"{r['C_sell']:.2f}",
                f"{r['annual_A']:.1f}次 / {mh_s}日",
            ]
            for c, val in enumerate(row):
                item = QTableWidgetItem(val)
                if r["meets_freq"] and r["meets_hold"]:
                    item.setBackground(Qt.GlobalColor.darkGreen)
                self.opt_table.setItem(i, c, item)

    def apply_selected(self) -> None:
        if not hasattr(self, "_opt_results") or not self._opt_results:
            QMessageBox.information(self, "提示", "请先运行参数搜索并选择一行。")
            return
        row = self.opt_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请在结果表中选择一行。")
            return
        r = self._opt_results[row]
        # 写回 config.json
        self.config["positions"]["A"]["buy_offset"] = r["A_buy"]
        self.config["positions"]["A"]["sell_offset"] = r["A_sell"]
        self.config["positions"]["B"]["buy_offset"] = r["B_buy"]
        self.config["positions"]["B"]["sell_offset"] = r["B_sell"]
        self.config["positions"]["C"]["buy_offset"] = r["C_buy"]
        self.config["positions"]["C"]["sell_offset"] = r["C_sell"]
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "已应用", "参数已写入 config.json，主界面将刷新。")
        if self.on_config_changed:
            self.on_config_changed()
