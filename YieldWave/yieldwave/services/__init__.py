"""服务层：纯计算与外部数据源解耦。

- point_estimator：纯计算（estimate_current_dp2 / yield_to_target_point / distance），
  无任何 IO、可独立单元测试。
- market_quote：H30269 指数点位的网络抓取与缓存（CSI 官方公开 JSON 接口）。
"""

from . import market_quote, point_estimator  # noqa: F401

__all__ = ["market_quote", "point_estimator"]
