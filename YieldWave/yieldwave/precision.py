"""Decimal 精度工具：股息率 / 阈值统一用 Decimal，显示统一 ROUND_HALF_UP。

为什么不用 float：
- 42 日中位数可能是偶数样本的均值，例如 4.835%；再叠加 +0.02 / -0.04 等半基点偏移，
  会出现 4.795% 这类“半个基点”的值。机械交易系统里 0.005 个百分点都可能影响信号，
  不能依赖 float 的偶然舍入结果。
规则：
1. 内部 median / threshold 计算与信号比较一律用 Decimal，保留完整精度。
2. 绝对不“先 round 再比较信号”——round 只发生在显示层。
3. 显示统一用 ROUND_HALF_UP，至少 3 位小数（原始 D/P2 仅 2 位时按原始精度显示）。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Union

Number = Union[None, str, int, float, Decimal]


def D(value: Number) -> Optional[Decimal]:
    """转为 Decimal，尽量保留原始精度。

    - str：直接 Decimal(s)（如 "4.94"）。
    - int：Decimal(int)。
    - float：用最短十进制字符串还原（Decimal(str(x))），避免二进制 float 表示污染
      原始精度。例如 Decimal(str(4.94)) == Decimal('4.94')，而非 4.9400000000000001…。
    - Decimal：原样返回。
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, str):
        s = value.strip()
        return Decimal(s) if s else None
    # int / float：用最短十进制字符串还原
    return Decimal(str(value))


def fmt_yield(value: Optional[Decimal], decimals: int = 3) -> str:
    """股息率显示：None 显示 '-'；否则统一 ROUND_HALF_UP 到 decimals 位小数。"""
    if value is None:
        return "-"
    q = Decimal(1).scaleb(-decimals)
    return str(value.quantize(q, rounding=ROUND_HALF_UP))
