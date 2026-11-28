"""策略市场状态与策略门槛。

由原 ``app/strategy/strategies.py`` 拆分而来；原模块保留为门面，
所有既有 ``from app.strategy.strategies import ...`` 导入保持可用。
"""

from __future__ import annotations
import logging

from datetime import date
from typing import Any
import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.fact import MarketIndex, MarketRegimeDaily, MarketTemperatureDaily

logger = logging.getLogger(__name__)

from app.strategy.specs import *  # noqa: F401,F403
from app.strategy.data import *  # noqa: F401,F403

async def load_market_context(
    db: AsyncSession,
    trade_date: date,
    index_code: str = BENCHMARK_INDEX_CODE,
) -> dict[str, Any]:
    """基准指数与市场阶段（均截至 T 日），用于 A4/C6 的市场门槛。

    只使用 <= trade_date 的数据；指数或阶段数据缺失时对应门槛放行
    （不因为基准/阶段缺失而让策略永远空结果）。
    """
    regime_stmt = (
        select(
            MarketRegimeDaily.trade_date,
            MarketRegimeDaily.regime_code,
            MarketRegimeDaily.regime_label,
            MarketRegimeDaily.regime_since,
            MarketRegimeDaily.regime_days,
        )
        .where(MarketRegimeDaily.trade_date <= trade_date)
        .order_by(MarketRegimeDaily.trade_date.desc())
        .limit(1)
    )
    try:
        regime_row = (await db.execute(regime_stmt)).first()
    except ProgrammingError:
        logger.warning("market_regime_daily 表不存在，市场阶段门槛按放行处理")
        regime_row = None
    regime_info: dict[str, Any] = {
        "regime_available": regime_row is not None,
        "regime_trade_date": regime_row[0].isoformat() if regime_row else None,
        "regime_code": regime_row[1] if regime_row else None,
        "regime_label": regime_row[2] if regime_row else None,
        "regime_since": regime_row[3].isoformat() if regime_row and regime_row[3] else None,
        "regime_days": int(regime_row[4]) if regime_row and regime_row[4] is not None else None,
    }

    # 市场温度：与“突破是否拥挤”直接相关；数据缺失时门槛放行。
    temp_stmt = (
        select(
            MarketTemperatureDaily.trade_date,
            MarketTemperatureDaily.temp_score,
        )
        .where(MarketTemperatureDaily.trade_date <= trade_date)
        .order_by(MarketTemperatureDaily.trade_date.desc())
        .limit(1)
    )
    try:
        temp_row = (await db.execute(temp_stmt)).first()
    except ProgrammingError:
        logger.warning("market_temperature_daily 表不存在，温度门槛按放行处理")
        temp_row = None
    temp_info: dict[str, Any] = {
        "temp_available": temp_row is not None and temp_row[1] is not None,
        "temp_trade_date": temp_row[0].isoformat() if temp_row else None,
        "temp_score": float(temp_row[1]) if temp_row and temp_row[1] is not None else None,
    }

    statement = (
        select(MarketIndex.trade_date, MarketIndex.close, MarketIndex.pct_chg)
        .where(MarketIndex.index_code == index_code)
        .where(MarketIndex.trade_date <= trade_date)
        .order_by(MarketIndex.trade_date.desc())
        .limit(60)
    )
    try:
        rows = (await db.execute(statement)).all()
    except ProgrammingError:
        logger.warning("market_indices 表不存在，市场状态门槛按放行处理")
        rows = []

    if not rows:
        logger.warning(
            "基准指数 %s 在 %s 前无数据，市场状态门槛按放行处理", index_code, trade_date
        )
        return {
            "available": False,
            "index_code": index_code,
            "c6_ok": True,
            "c3_ok": True,
            "ma20_history": [],
            "ma20_above_flags": [],
            **regime_info,
            **temp_info,
        }

    ordered = list(reversed(rows))
    closes = [
        float(close) for _, close, _ in ordered
        if close is not None and float(close) > 0
    ]
    last_close = closes[-1] if closes else None
    ma20 = round(sum(closes[-20:]) / 20.0, 4) if len(closes) >= 20 else None
    ma60 = round(sum(closes[-60:]) / 60.0, 4) if len(closes) >= 60 else None
    raw_pct = ordered[-1][2]
    last_pct = float(raw_pct) if raw_pct is not None else None
    c6_ok = True if (last_close is None or ma20 is None) else last_close >= ma20 * 0.995
    # C3 用双均线闸门：中期趋势（MA60）与短期趋势（MA20）同时向上才开仓
    c3_ok = c6_ok if (last_close is None or ma60 is None) else (
        c6_ok and last_close >= ma60
    )
    # 最近 10 个交易日的 MA20 序列与“收盘站上 MA20×0.995”标记（按时间升序），
    # 供 C3 的“连续 N 日站稳 + MA20 上行”确认使用。
    ma20_history: list[float] = []
    ma20_above_flags: list[bool] = []
    if len(closes) >= 20:
        for offset in range(min(10, len(closes) - 19) - 1, -1, -1):
            end = len(closes) - offset
            window = closes[end - 20:end]
            if len(window) < 20:
                continue
            day_ma20 = sum(window) / 20.0
            ma20_history.append(round(day_ma20, 4))
            ma20_above_flags.append(closes[end - 1] >= day_ma20 * 0.995)

    return {
        "available": True,
        "index_code": index_code,
        "close": round(last_close, 2) if last_close is not None else None,
        "ma20": ma20,
        "ma60": ma60,
        "pct_chg": last_pct,
        "c6_ok": c6_ok,
        "c3_ok": c3_ok,
        "ma20_history": ma20_history,
        "ma20_above_flags": ma20_above_flags,
        **regime_info,
        **temp_info,
    }


def _market_temp_ok(market: dict, temp_max: float) -> bool:
    """市场温度是否低于策略上限；温度缺失时放行。"""
    if not market.get("temp_available"):
        return True
    score = market.get("temp_score")
    if score is None:
        return True
    try:
        return float(score) < float(temp_max)
    except (TypeError, ValueError):
        return True


def _regime_not_risk_off(market: dict) -> bool:
    """市场状态是否不是“避险下跌”；阶段数据缺失时放行。"""
    if not market.get("regime_available"):
        return True
    return market.get("regime_code") != "risk_off"


def _ma20_confirm_ok(market: dict, confirm_days: int) -> bool:
    """收盘是否连续 ``confirm_days`` 日站上 MA20×0.995；数据不足时放行。"""
    flags = market.get("ma20_above_flags") or []
    days = max(1, int(confirm_days or 1))
    if len(flags) < days:
        return True
    return all(bool(flag) for flag in flags[-days:])


def _ma20_slope_ok(market: dict, slope_days: int) -> bool:
    """MA20 是否较 ``slope_days`` 个交易日前抬升；数据不足时放行。"""
    history = market.get("ma20_history") or []
    days = max(1, int(slope_days or 1))
    if len(history) <= days:
        return True
    try:
        return float(history[-1]) > float(history[-1 - days])
    except (TypeError, ValueError):
        return True


def _gate_param(algo_code: str, params: dict | None, key: str, fallback: float) -> float:
    """读取门槛参数；缺省时用策略默认值，非法值退回 fallback。"""
    try:
        default = get_spec(algo_code).defaults.get(key, fallback)
    except Exception:  # noqa: BLE001 - 元信息异常不能拖垮门槛
        default = fallback
    try:
        return float((params or {}).get(key, default))
    except (TypeError, ValueError):
        return float(fallback)


def market_gate_payload(
    market: dict | None, algo_code: str, params: dict | None = None
) -> dict[str, Any] | None:
    """run 级市场门槛摘要（A4/C1/C3/C6），用于 0 信号时解释原因。"""
    if not market:
        return None
    available = bool(market.get("available", False))
    payload: dict[str, Any] = {
        "index_code": market.get("index_code", BENCHMARK_INDEX_CODE),
        "available": available,
        "close": jsonable(market.get("close")),
        "ma20": jsonable(market.get("ma20")),
        "ma60": jsonable(market.get("ma60")),
        "pct_chg": jsonable(market.get("pct_chg")),
    }
    if algo_code == "a4":
        regime_available = bool(market.get("regime_available", False))
        regime_code = market.get("regime_code")
        passed = True if not regime_available else regime_code in A4_DEFENSIVE_REGIMES
        payload.update(
            {
                "passed": passed,
                "rule": (
                    "市场阶段数据缺失，防御型门槛按放行处理"
                    if not regime_available
                    else "仅在防御型市场阶段启用（震荡筑底/向下调整/避险下跌）"
                ),
                "regime_code": regime_code,
                "regime_label": market.get("regime_label"),
                "regime_trade_date": market.get("regime_trade_date"),
                "regime_since": market.get("regime_since"),
                "regime_days": market.get("regime_days"),
                "regime_available": regime_available,
                "threshold": sorted(A4_DEFENSIVE_REGIMES),
            }
        )
        return payload
    if algo_code == "c1":
        temp_max = _gate_param("c1", params, "temp_max", 70.0)
        temp_ok = _market_temp_ok(market, temp_max)
        regime_ok = _regime_not_risk_off(market)
        payload.update(
            {
                "passed": bool(temp_ok and regime_ok),
                "rule": f"市场温度 < {temp_max:g} 且市场阶段不是避险下跌",
                "threshold": temp_max,
                "temp_score": jsonable(market.get("temp_score")),
                "temp_available": bool(market.get("temp_available")),
                "temp_trade_date": market.get("temp_trade_date"),
                "regime_code": market.get("regime_code"),
                "regime_label": market.get("regime_label"),
                "regime_available": bool(market.get("regime_available")),
            }
        )
        return payload
    if algo_code == "c3":
        confirm_days = int(_gate_param("c3", params, "confirm_days", 2.0))
        slope_days = int(_gate_param("c3", params, "ma20_slope_days", 5.0))
        confirm_ok = _ma20_confirm_ok(market, confirm_days)
        slope_ok = _ma20_slope_ok(market, slope_days)
        base_ok = True if not available else bool(market.get("c3_ok", True))
        payload.update(
            {
                "passed": bool(base_ok and confirm_ok and slope_ok),
                "rule": (
                    f"沪深300 ≥ MA20×0.995 且 ≥ MA60；连续 {confirm_days} 日站稳、"
                    f"MA20 较 {slope_days} 日前抬升"
                ),
                "threshold": None,
                "confirm_days": confirm_days,
                "ma20_above_flags": jsonable(market.get("ma20_above_flags") or []),
                "ma20_history": jsonable(market.get("ma20_history") or []),
                "ma20_slope_days": slope_days,
            }
        )
        return payload
    if not available:
        payload.update({"passed": True, "rule": "指数数据缺失，市场门槛按放行处理"})
        return payload
    ma20 = market.get("ma20")
    payload.update(
        {
            "passed": bool(market.get("c6_ok", True)),
            "rule": "沪深300 收盘 ≥ MA20 × 0.995",
            "threshold": None if ma20 is None else round(float(ma20) * 0.995, 4),
        }
    )
    return payload

def _market_flag(df: pd.DataFrame, key: str) -> bool:
    """读取市场状态门槛；无 attrs（单测/回测）或指数缺失时放行。"""
    market = df.attrs.get("market") or {}
    return bool(market.get(key, True))

__all__ = [
    "load_market_context",
    "market_gate_payload",
    "_market_flag",
    "_market_temp_ok",
    "_regime_not_risk_off",
    "_ma20_confirm_ok",
    "_ma20_slope_ok",
]
