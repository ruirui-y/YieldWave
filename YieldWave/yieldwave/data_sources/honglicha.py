"""红利查（honglicha.com）数据源解析。

发现（已写入 README）：
- H30269 详情页 https://www.honglicha.com/H30269/ 把历史估值序列**服务端渲染**进页面，
  作为 ECharts 的 option JSON 内嵌在 <script> 里（window.initDetailCharts(chartsData)）。
- 没有公开的 JSON/API 接口，也没有 XHR/Fetch 请求；数据就是页面 HTML 的一部分。
- 因此解析方式：请求页面 HTML（带浏览器 UA，无需登录/验证码），用正则定位每个
  `option:` 后的平衡 JSON，找到含 "DP2"/"DP1" 的图（股息率）与含 "PE2"/"PE1" 的图（市盈率），
  按 xAxis 的日期对齐，得到 dividend_yield_1/2、pe_1/2。
- 该页面当前内嵌 **115 个交易日（2026-03-25 ~ 2026-09-02，约 5.7 个月）** 的历史序列；
  经重新调查（detail.js 仅做渲染、无数据接口、页面无时间范围选择器、所有图表块日期数一致），
  **公开页面可访问的 D/P2 历史最长即 115 个交易日，并非一年**。程序首次运行即写入本地库，
  之后每日增量 UPSERT；随运行时间累积，本地历史会自然变长。
- 页面不提供指数点位（close），因此该字段为 None。

健壮性：若页面结构变化导致解析不到任何记录，parse_html 返回空列表，调用方据此显示
“红利查数据抓取失败”并保留本地旧数据，绝不清空历史。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import urllib.request
from typing import List, Optional, Tuple

from ..models import ValuationRecord
from ..precision import D

HONGLICHA_URL = "https://www.honglicha.com/H30269/"
INDEX_CODE = "H30269"
INDEX_NAME = "中证红利低波"


def fetch_html(user_agent: str, timeout: int = 30) -> str:
    """请求页面 HTML。失败时抛出异常（由调用方捕获并提示失败）。"""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    req = urllib.request.Request(HONGLICHA_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return resp.read().decode("utf-8", "replace")


def _extract_option_objects(html: str) -> List[dict]:
    """提取页面里所有 ECharts option JSON 对象。"""
    objs: List[dict] = []
    for m in re.finditer(r"option:", html):
        j = html.find("{", m.end())
        if j == -1:
            continue
        depth = 0
        end = None
        for i in range(j, len(html)):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        try:
            objs.append(json.loads(html[j : end + 1]))
        except json.JSONDecodeError:
            continue
    return objs


def _find_series(chart: dict, *keywords: str) -> Optional[list]:
    for s in chart.get("series", []):
        name = s.get("name", "")
        if any(kw in name for kw in keywords):
            return s.get("data", [])
    return None


def parse_html(html: str) -> List[ValuationRecord]:
    """从页面 HTML 解析出历史估值记录。解析不到则返回空列表。"""
    if not html or "initDetailCharts" not in html:
        return []
    objs = _extract_option_objects(html)
    dp_chart = None
    pe_chart = None
    for o in objs:
        if dp_chart is None and _find_series(o, "DP2"):
            dp_chart = o
        if pe_chart is None and _find_series(o, "PE2"):
            pe_chart = o
    if dp_chart is None:
        return []

    dates = dp_chart.get("xAxis", {}).get("data", [])
    if not dates:
        return []

    dy1 = _find_series(dp_chart, "DP1") or [None] * len(dates)
    dy2 = _find_series(dp_chart, "DP2") or [None] * len(dates)
    pe1 = _find_series(pe_chart, "PE1") if pe_chart else [None] * len(dates)
    pe2 = _find_series(pe_chart, "PE2") if pe_chart else [None] * len(dates)

    records: List[ValuationRecord] = []
    fetched_at = _dt.datetime.now().isoformat(timespec="seconds")
    for i, d in enumerate(dates):
        try:
            dt = _dt.date.fromisoformat(str(d)[:10])
        except Exception:
            continue
        records.append(
            ValuationRecord(
                date=dt,
                index_code=INDEX_CODE,
                index_name=INDEX_NAME,
                dividend_yield_1=D(dy1[i]) if i < len(dy1) else None,
                dividend_yield_2=D(dy2[i]) if i < len(dy2) else None,
                pe_1=D(pe1[i]) if i < len(pe1) else None,
                pe_2=D(pe2[i]) if i < len(pe2) else None,
                close=None,  # 红利查详情页不提供指数点位
                source="honglicha",
                fetched_at=fetched_at,
            )
        )
    return records


def fetch_valuation_records(user_agent: str) -> Tuple[List[ValuationRecord], Optional[str]]:
    """抓取并解析。返回 (records, error_msg)。error_msg 为 None 表示成功。"""
    try:
        html = fetch_html(user_agent)
    except Exception as exc:  # 网络/HTTP 错误
        return [], f"红利查数据抓取失败（网络错误）：{exc}"
    records = parse_html(html)
    if not records:
        return [], "红利查数据抓取失败：页面结构可能已变化，未能解析到历史序列，已保留本地旧数据。"
    return records, None
