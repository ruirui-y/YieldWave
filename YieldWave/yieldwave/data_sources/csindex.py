"""可选备用数据源：中证官方 / AKShare 校验。

仅用于“最近约 20 个交易日”的股息率2 与红利查做交叉校验，不参与主历史库写入，
也绝不与红利查口径的数据混合。需要用户自行安装 akshare（pip install akshare）。

若未安装或接口变化，返回 error_msg，由调用方显示“备用数据源不可用”，不影响主流程。
"""

from __future__ import annotations

from typing import List, Optional, Tuple


def fetch_csindex_dp2(symbol: str = "H30269") -> Tuple[List[Tuple[str, float]], Optional[str]]:
    """返回 [(date_str, dividend_yield_2), ...]（按日期升序）与错误信息。"""
    try:
        import akshare as ak  # type: ignore
    except ImportError:
        return [], "未安装 akshare（可选备用数据源，pip install akshare 后可启用校验）"

    try:
        df = ak.stock_zh_index_value_csindex(symbol=symbol)
    except Exception as exc:  # 接口变动 / 网络
        return [], f"AKShare 接口调用失败：{exc}"

    if df is None or len(df) == 0:
        return [], "AKShare 返回空数据"

    # 兼容不同列名：找日期列与股息率列
    cols = list(df.columns)
    date_col = next((c for c in cols if "日期" in str(c) or "date" in str(c).lower()), cols[0])
    dp_col = None
    for c in cols:
        cs = str(c)
        if "股息" in cs and ("2" in cs or "计算" in cs):
            dp_col = c
            break
    if dp_col is None:
        for c in cols:
            if "股息" in str(c):
                dp_col = c
                break
    if dp_col is None:
        return [], f"AKShare 返回中未找到股息率列，列名：{cols}"

    out: List[Tuple[str, float]] = []
    for _, row in df.iterrows():
        try:
            d = str(row[date_col])[:10]
            v = float(row[dp_col])
            out.append((d, v))
        except Exception:
            continue
    if not out:
        return [], "AKShare 解析股息率为空"
    return out, None
