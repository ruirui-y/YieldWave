#!/usr/bin/env python
"""YieldWave 启动入口。

启动流程：
1. 加载配置
2. 打开 SQLite（建表，初始化仓位状态）
3. 若今天尚未更新过，自动尝试更新红利查一次（网络失败不影响启动，沿用本地数据）
4. 应用全局深色主题
5. 创建主窗口并显示（主窗口会异步刷新当前指数点位）
"""

from __future__ import annotations

import datetime as _dt
import sys

from PyQt6.QtWidgets import QApplication

from yieldwave.config import ensure_dirs, get_user_agent, load_config
from yieldwave.database import Database
from yieldwave.data_sources import honglicha
from yieldwave.ui import MainWindow
from yieldwave.ui.theme import apply_dark_theme


def auto_update_once(db: Database, config) -> None:
    """启动时的自动更新（受每日抓取次数限制，且不因“最新数据不是今天”就每次重抓）。

    - 今天已拉取过（无论成功与否）→ 跳过，避免每启动一次就请求一次。
    - 当天已达 max_per_day → 跳过。
    - 否则尝试抓取并写入 fetch_log（成功/失败都记录）。
    """
    today = _dt.date.today().isoformat()
    source = config.get("source", "honglicha")
    max_per_day = config.get("update", {}).get("max_per_day", 4)

    if db.latest_date() == today:
        print(f"[启动] 今天 ({today}) 数据已是最新，跳过自动更新。")
        return
    if db.fetch_count_today(source) >= max_per_day:
        print(f"[启动] 今日自动更新已达上限（{max_per_day} 次），跳过。")
        return
    if db.fetch_count_today(source) >= 1 and db.last_fetch_success(source) is True:
        print(f"[启动] 今天已成功抓取过一次，跳过重复自动更新（最新 {db.latest_date()}）。")
        return

    try:
        records, err = honglicha.fetch_valuation_records(get_user_agent(config))
        if err:
            db.log_fetch(source, False, db.latest_date(), 0)
            print(f"[启动] 自动更新跳过：{err}（已保留本地历史数据）")
            return
        n = db.upsert_many(records)
        db.log_fetch(source, True, records[-1].date.isoformat(), n)
        print(f"[启动] 自动更新完成，新增/更新 {n} 条（最新 {records[-1].date.isoformat()}）。")
    except Exception as exc:  # 任何异常都不影响程序启动
        db.log_fetch(source, False, db.latest_date(), 0)
        print(f"[启动] 自动更新失败，保留本地数据继续运行：{exc}")


def main() -> None:
    config = load_config()
    ensure_dirs()
    db = Database()
    db.ensure_positions(config)  # 完整配置：同时初始化 A/B/C 与核心 CORE1/2/3 仓位
    auto_update_once(db, config)

    app = QApplication(sys.argv)
    app.setApplicationName("YieldWave")
    apply_dark_theme(app)  # 全局深色 QSS + QPalette 兜底
    window = MainWindow(db, config)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
