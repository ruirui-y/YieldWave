"""配置加载与路径管理。

所有可调参数都来自 config/config.json，代码里不写死任何策略数字。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

# 项目根目录：yieldwave 包的父目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.json")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
EXPORTS_DIR = os.path.join(PROJECT_ROOT, "exports")
DB_PATH = os.path.join(DATA_DIR, "yieldwave.db")

# 默认配置（当 config.json 缺失或损坏时兜底，保证程序能启动）
DEFAULT_CONFIG: Dict[str, Any] = {
    "index_code": "H30269",
    "index_name": "中证红利低波",
    "index_name_en": "CSI Dividend Low Volatility",
    "source": "honglicha",
    "primary_window": 42,
    "windows": {"M20": 20, "M42": 42, "M60": 60},
    "core_percent": 60,
    "positions": {
        "A": {"label": "A仓", "percent": 20, "buy_offset": 0.02, "sell_offset": -0.01},
        "B": {"label": "B仓", "percent": 12, "buy_offset": 0.07, "sell_offset": -0.04},
        "C": {"label": "C仓", "percent": 8, "buy_offset": 0.12, "sell_offset": -0.11},
    },
    "auto_confirm": False,
    "realtime_estimate_mode": False,
    "csindex_verify_tolerance": 0.02,
    "csindex_enabled": False,
    "backtest_windows": [20, 30, 42, 50, 60],
    "update": {
        "max_per_day": 4,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    },
}


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """加载 config.json；缺失或损坏时回退到默认配置并打印警告。"""
    if not os.path.exists(path):
        print(f"[配置] 未找到 {path}，使用内置默认配置。")
        return dict(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 与默认配置做浅合并，避免缺字段导致 KeyError
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        for k, v in DEFAULT_CONFIG.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**v, **merged[k]}
        return merged
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[配置] 读取 {path} 失败：{exc}，使用内置默认配置。")
        return dict(DEFAULT_CONFIG)


def ensure_dirs() -> None:
    """确保 data / exports 目录存在。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(EXPORTS_DIR, exist_ok=True)


def get_user_agent(cfg: Dict[str, Any]) -> str:
    return cfg.get("update", {}).get("user_agent", DEFAULT_CONFIG["update"]["user_agent"])
