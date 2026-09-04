"""H30269 指数点位数据源（中证指数官网公开 JSON 接口）。

只使用公开数据，不绕登录、不破解、不需要验证码。
任何网络/解析失败都返回 (None, error_msg)，绝不抛异常影响主流程。

数据源（实测可用）：
1. 实时盘中点位：
   GET https://www.csindex.com.cn/csindex-home/perf/index-perf-oneday?indexCode=H30269
   返回 data.intraDayHeader.{tradeDate, tradeTime, current, closePre}
2. 历史日收盘点位：
   GET https://www.csindex.com.cn/csindex-home/perf/index-perf
       ?indexCode=H30269&startDate=YYYYMMDD&endDate=YYYYMMDD
   返回 data: [{tradeDate:"YYYYMMDD", close:..., open:..., high:..., low:...}, ...]

为何选这两个接口（技术决策说明）：
- 为什么是这个方案：用户要求 "本周每个买卖股息率阈值对应多少点"，
  必须拿到 anchor_date 当天收盘点位 与 当前盘中点位；CSI 官网 JSON 接口是公开、稳定、
  返回结构化数据的最直接来源。
- 底层是什么：HTTP GET 返回的 JSON 由中证官网前端 SPA 消费（无需 cookie/登录），
  服务端按交易日聚合，startDate/endDate 用 YYYYMMDD 格式。
- 如果换你会怎么学：去看 www.csindex.com.cn 详情页的 Network XHR，
  能直接看到这两个 endpoint 的请求/响应；CSI 官方未公开文档，但接口稳定。
"""

from __future__ import annotations

import datetime as _dt
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..precision import D

BASE = "https://www.csindex.com.cn/csindex-home"
INDEX_CODE = "H30269"
REFERER = "https://www.csindex.com.cn/zh-CN/indices/index-detail/H30269"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@dataclass(frozen=True)
class CurrentQuote:
    """实时盘中点位快照。"""

    trade_date: str   # "2026-09-04"
    trade_time: str   # "14:32:12"
    current: Optional[float]   # 当前点位（盘中实时）
    pre_close: Optional[float]  # 上一交易日收盘
    source: str = "csindex"


def _to_dashless(d) -> str:
    """任意日期 -> "YYYYMMDD" 字符串。"""
    if isinstance(d, _dt.date):
        return d.strftime("%Y%m%d")
    s = str(d).strip()
    if "-" in s:
        return s.replace("-", "")
    if "/" in s:
        return s.replace("/", "")
    return s


def _to_dashed(s) -> str:
    """YYYYMMDD -> YYYY-MM-DD。"""
    s = str(s).strip()
    if "-" in s or "/" in s:
        return s.replace("/", "-")[:10]
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _http_get(url: str, timeout: int = 15) -> Tuple[Optional[dict], Optional[str]]:
    """GET 并解析 JSON。失败统一返回 (None, error_msg)。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Referer": REFERER,
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as exc:  # 网络/HTTP/超时
        return None, f"网络请求失败：{exc}"
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc}"
    return obj, None


def fetch_current_quote(index_code: str = INDEX_CODE, timeout: int = 15) -> Tuple[Optional[CurrentQuote], Optional[str]]:
    """拉取盘中实时点位。返回 (quote, error_msg)。"""
    url = f"{BASE}/perf/index-perf-oneday?indexCode={urllib.parse.quote(index_code)}"
    obj, err = _http_get(url, timeout=timeout)
    if err:
        return None, err
    if not obj or obj.get("code") != "200" or not obj.get("data"):
        return None, f"接口返回异常：{obj.get('msg') if obj else '空响应'}"
    data = obj["data"]
    header = data.get("intraDayHeader") or {}
    if not header:
        return None, "响应缺少 intraDayHeader"
    try:
        cur = float(header["current"]) if header.get("current") is not None else None
    except (TypeError, ValueError):
        cur = None
    try:
        pre = float(header["closePre"]) if header.get("closePre") is not None else None
    except (TypeError, ValueError):
        pre = None
    return CurrentQuote(
        trade_date=_to_dashed(header.get("tradeDate", "")),
        trade_time=header.get("tradeTime", ""),
        current=cur,
        pre_close=pre,
    ), None


def fetch_close_range(
    start_date,
    end_date,
    index_code: str = INDEX_CODE,
    timeout: int = 20,
) -> Tuple[Dict[str, float], Optional[str]]:
    """拉取 [start_date, end_date] 区间内每日 close。

    返回 {"YYYY-MM-DD": close_float, ...}（按日期升序合并）。
    失败返回 ({}, error_msg)。
    """
    sd = _to_dashless(start_date)
    ed = _to_dashless(end_date)
    url = (
        f"{BASE}/perf/index-perf?indexCode={urllib.parse.quote(index_code)}"
        f"&startDate={sd}&endDate={ed}"
    )
    obj, err = _http_get(url, timeout=timeout)
    if err:
        return {}, err
    if not obj or obj.get("code") != "200" or not obj.get("data"):
        return {}, f"接口返回异常：{obj.get('msg') if obj else '空响应'}"
    data = obj["data"]
    out: Dict[str, float] = {}
    for row in data:
        td = row.get("tradeDate")
        if not td:
            continue
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if close is None or close <= 0:
            continue
        out[_to_dashed(td)] = close
    return out, None


def fetch_close_on(date, index_code: str = INDEX_CODE, timeout: int = 15) -> Tuple[Optional[float], Optional[str]]:
    """拉取单日 close。返回 (close_float, error_msg)。"""
    d = _to_dashless(date)
    mapping, err = fetch_close_range(d, d, index_code=index_code, timeout=timeout)
    if err:
        return None, err
    if not mapping:
        return None, f"{date} 当日无点位数据"
    # 取该日 close
    dashed = _to_dashed(d)
    return mapping.get(dashed), None


def closest_close_before(mapping: Dict[str, float], target_date) -> Optional[Tuple[str, float]]:
    """在 mapping 里找 <= target_date 的最近一日的 close。

    mapping 为 {"YYYY-MM-DD": float, ...}。
    返回 (date_str, close_float) 或 None。
    """
    target = _to_dashed(target_date)
    candidates = sorted(
        ((d, c) for d, c in mapping.items() if d <= target),
        key=lambda x: x[0],
    )
    if not candidates:
        return None
    return candidates[-1]


def to_decimal_close(mapping: Dict[str, float]) -> Dict[str, "Decimal"]:
    """float close -> Decimal close（统一精度口径）。"""
    return {d: D(c) for d, c in mapping.items() if c is not None}
