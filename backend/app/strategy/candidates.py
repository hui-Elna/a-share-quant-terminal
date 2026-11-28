"""策略候选池构建。

由原 ``app/strategy/strategies.py`` 拆分而来；原模块保留为门面，
所有既有 ``from app.strategy.strategies import ...`` 导入保持可用。
"""

from __future__ import annotations
from datetime import date
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.errors import DataUnavailableError, InvalidParamError

from app.strategy.specs import *  # noqa: F401,F403
from app.strategy.data import *  # noqa: F401,F403
from app.strategy.market import *  # noqa: F401,F403
from app.strategy.conditions import *  # noqa: F401,F403
from app.strategy.ranking import *  # noqa: F401,F403

async def build_candidates(
    algo_code: str,
    trade_date: date,
    params: dict | None = None,
    db: AsyncSession | None = None,
) -> pd.DataFrame:
    """构建某策略某日的候选池（一次横截面 SQL；c1–c3 额外一次窗口查询）。

    返回 DataFrame 的 attrs 中带：
      - ``universe_count``  当日全市场有行情的标的数（契约 5.2 的 run.universe_count）
      - ``eligible_count``  通过统一前置过滤后的标的数
      - ``calendar_source`` "db" / "fallback"
      - ``trade_date``      回测/详情页需要的日期
      - ``market``          A4/C1/C3/C6 的沪深300、市场阶段与温度（其他策略不取）
    """
    spec = get_spec(algo_code)
    normalized = _validate_params(spec.code, params)
    if db is None:
        raise InvalidParamError("build_candidates 需要传入 AsyncSession")
    if not isinstance(trade_date, date):
        raise InvalidParamError(f"trade_date 必须是 date，收到: {type(trade_date).__name__}")

    frame = await _fetch_cross_section(db, trade_date)
    if len(frame) == 0:
        raise DataUnavailableError(
            f"{trade_date.isoformat()} 无个股行情数据",
            next_action="POST /api/v1/admin/ingest/daily_snapshot",
            detail={"trade_date": trade_date.isoformat()},
        )

    universe_count = int(len(frame))
    open_dates = await _load_open_dates(db, trade_date)
    eligible = apply_common_filters(frame, trade_date, open_dates)
    if spec.code in _WINDOW_STRATEGIES:
        eligible = await _enrich_history(
            eligible, trade_date, db, spec.code, normalized
        )
    _assert_data_ready(spec.code, eligible)

    market = None
    if spec.code in {"a4", "c1", "c3", "c6"}:
        market = await load_market_context(db, trade_date)

    # 最终出口再统一 coerce 一次：保证调用方拿到的数值列一定是 float64
    pool = coerce_numeric_frame(filter_by_strategy(spec.code, eligible, normalized))
    pool.attrs.update(
        {
            "trade_date": trade_date,
            "universe_count": universe_count,
            "eligible_count": int(len(eligible)),
            "calendar_source": "db" if open_dates else "fallback",
            "algo": spec.code,
            "params": dict(normalized),
        }
    )
    if market is not None:
        pool.attrs["market"] = market
    return pool

__all__ = [
    "build_candidates",
]
