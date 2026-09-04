"""数据管理页面。

功能：立即更新红利查 / 查看数据库 / 最早·最新·总条数 / 导出CSV / 导入CSV /
去重 / 缺失交易日检查 / 手工增加或修改一条数据。
不提供“一键清库”；任何删除都必须二次确认。
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import EXPORTS_DIR, get_user_agent
from ..database import Database
from ..models import ValuationRecord


class _EditRowDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, rec: Optional[ValuationRecord] = None):
        super().__init__(parent)
        self.setWindowTitle("手工增加 / 修改一条数据")
        fl = QFormLayout(self)
        self.date = QLineEdit(rec.date.isoformat() if rec else "")
        self.dy1 = QDoubleSpinBox(); self.dy1.setRange(0, 100); self.dy1.setDecimals(4)
        self.dy2 = QDoubleSpinBox(); self.dy2.setRange(0, 100); self.dy2.setDecimals(4)
        self.pe1 = QDoubleSpinBox(); self.pe1.setRange(0, 1000); self.pe1.setDecimals(4)
        self.pe2 = QDoubleSpinBox(); self.pe2.setRange(0, 1000); self.pe2.setDecimals(4)
        self.close = QDoubleSpinBox(); self.close.setRange(0, 100000); self.close.setDecimals(2)
        if rec:
            self.dy1.setValue(rec.dividend_yield_1 or 0)
            self.dy2.setValue(rec.dividend_yield_2 or 0)
            self.pe1.setValue(rec.pe_1 or 0)
            self.pe2.setValue(rec.pe_2 or 0)
            self.close.setValue(rec.close or 0)
        fl.addRow("日期 (YYYY-MM-DD):", self.date)
        fl.addRow("股息率1 (D/P1):", self.dy1)
        fl.addRow("股息率2 (D/P2):", self.dy2)
        fl.addRow("PE1:", self.pe1)
        fl.addRow("PE2:", self.pe2)
        fl.addRow("指数点位 close (可空):", self.close)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        fl.addRow(btns)


class DataWidget(QWidget):
    def __init__(
        self,
        db: Database,
        config: Dict,
        on_update: Optional[Callable[[], tuple]] = None,
        on_changed: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.db = db
        self.config = config
        self.on_update = on_update
        self.on_changed = on_changed
        self._build_ui()
        self.refresh_stats()

    def _build_ui(self) -> None:
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.btn_update = QPushButton("立即更新红利查")
        self.btn_update.clicked.connect(self.update_now)
        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.clicked.connect(self.export_csv)
        self.btn_import = QPushButton("导入 CSV")
        self.btn_import.clicked.connect(self.import_csv)
        self.btn_dedupe = QPushButton("数据去重")
        self.btn_dedupe.clicked.connect(self.dedupe)
        self.btn_check = QPushButton("缺失交易日检查")
        self.btn_check.clicked.connect(self.check_missing)
        self.btn_edit = QPushButton("手工增加/修改一条")
        self.btn_edit.clicked.connect(self.edit_row)
        top.addWidget(self.btn_update)
        top.addWidget(self.btn_export)
        top.addWidget(self.btn_import)
        top.addWidget(self.btn_dedupe)
        top.addWidget(self.btn_check)
        top.addWidget(self.btn_edit)
        top.addStretch(1)
        v.addLayout(top)

        self.stats = QLabel("统计信息加载中…")
        v.addWidget(self.stats)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels(
            ["date", "code", "name", "D/P1", "D/P2", "PE1", "PE2", "close", "source"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.table)

        self.status = QLabel("")
        v.addWidget(self.status)

    def refresh_stats(self) -> None:
        c = self.db.count()
        e = self.db.earliest_date()
        l = self.db.latest_date()
        self.stats.setText(f"总条数：{c}    最早日期：{e}    最新日期：{l}")
        rows = self.db.get_all_valuations()
        self.table.setRowCount(min(len(rows), 2000))
        for i, r in enumerate(rows[-2000:]):
            self.table.setItem(i, 0, QTableWidgetItem(r.date.isoformat()))
            self.table.setItem(i, 1, QTableWidgetItem(r.index_code))
            self.table.setItem(i, 2, QTableWidgetItem(r.index_name))
            self.table.setItem(i, 3, QTableWidgetItem(f"{r.dividend_yield_1:.2f}" if r.dividend_yield_1 is not None else "-"))
            self.table.setItem(i, 4, QTableWidgetItem(f"{r.dividend_yield_2:.2f}" if r.dividend_yield_2 is not None else "-"))
            self.table.setItem(i, 5, QTableWidgetItem(f"{r.pe_1:.2f}" if r.pe_1 is not None else "-"))
            self.table.setItem(i, 6, QTableWidgetItem(f"{r.pe_2:.2f}" if r.pe_2 is not None else "-"))
            self.table.setItem(i, 7, QTableWidgetItem(f"{r.close:.2f}" if r.close is not None else "-"))
            self.table.setItem(i, 8, QTableWidgetItem(r.source))

    def update_now(self) -> None:
        if self.on_update is None:
            self.status.setText("更新函数未注入。")
            return
        count, err = self.on_update()
        if err:
            self.status.setText(f"⚠️ {err}")
        else:
            self.status.setText(f"✅ 已更新，新增/更新 {count} 条。")
        self.refresh_stats()
        if self.on_changed:
            self.on_changed()

    def export_csv(self) -> None:
        import os

        path = os.path.join(EXPORTS_DIR, "H30269_dividend_yield_history.csv")
        n = self.db.export_csv(path)
        self.status.setText(f"已导出 {n} 条到 {path}")
        if self.on_changed:
            self.on_changed()

    def import_csv(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(self, "选择 CSV", "", "CSV (*.csv)")
        if not path:
            return
        # 导入是 UPSERT，不是清空，因此不需要 replace。但若用户坚持整表替换，需二次确认。
        reply = QMessageBox.question(
            self, "导入方式",
            "CSV 导入按日期 UPSERT（已存在日期更新，新日期插入，不删除旧历史）。\n"
            "是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        n = self.db.import_csv(path, replace=False)
        self.status.setText(f"已导入/更新 {n} 条。")
        self.refresh_stats()
        if self.on_changed:
            self.on_changed()

    def dedupe(self) -> None:
        n = self.db.dedupe()
        self.status.setText(f"去重完成，处理重复日期 {n} 组。")
        self.refresh_stats()

    def check_missing(self) -> None:
        gaps = self.db.missing_trading_days_check()
        if not gaps:
            self.status.setText("缺失交易日检查：未发现明显缺口。")
        else:
            self.status.setText("发现可能的缺失间隔：\n" + "\n".join(gaps[:20]))

    def edit_row(self) -> None:
        dlg = _EditRowDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        date_str = dlg.date.text().strip()
        if not date_str:
            QMessageBox.warning(self, "错误", "日期不能为空。")
            return
        rec = ValuationRecord(
            date=date_str,
            index_code=self.config.get("index_code", "H30269"),
            index_name=self.config.get("index_name", ""),
            dividend_yield_1=dlg.dy1.value() or None,
            dividend_yield_2=dlg.dy2.value() or None,
            pe_1=dlg.pe1.value() or None,
            pe_2=dlg.pe2.value() or None,
            close=dlg.close.value() or None,
            source="manual",
        )
        self.db.upsert_valuation(rec)
        self.status.setText(f"已保存 {date_str}。")
        self.refresh_stats()
        if self.on_changed:
            self.on_changed()
