#!/usr/bin/env python
"""YieldWave 启动入口。

启动流程：
1. 加载配置
2. 打开 SQLite（建表，初始化仓位状态）
3. 若今天尚未更新过，自动尝试更新红利查一次（网络失败不影响启动，沿用本地数据）
4. 创建主窗口并显示
"""

from __future__ import annotations

import datetime as _dt
import sys

from PyQt6.QtWidgets import QApplication

from yieldwave.config import ensure_dirs, get_user_agent, load_config
from yieldwave.database import Database
from yieldwave.data_sources import honglicha
from yieldwave.ui import MainWindow


def auto_update_once(db: Database, config) -> None:
    today = _dt.date.today().isoformat()
    if db.latest_date() == today:
        print(f"[启动] 今天 ({today}) 已更新过，跳过自动更新。")
        return
    try:
        records, err = honglicha.fetch_valuation_records(get_user_agent(config))
        if err:
            print(f"[启动] 自动更新跳过：{err}（已保留本地历史数据）")
            return
        n = db.upsert_many(records)
        print(f"[启动] 自动更新完成，新增/更新 {n} 条（最新 {records[-1].date.isoformat()}）。")
    except Exception as exc:  # 任何异常都不影响程序启动
        print(f"[启动] 自动更新失败，保留本地数据继续运行：{exc}")


def main() -> None:
    config = load_config()
    ensure_dirs()
    db = Database()
    db.ensure_positions(config["positions"])
    auto_update_once(db, config)

    app = QApplication(sys.argv)
    app.setApplicationName("YieldWave")
    window = MainWindow(db, config)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
