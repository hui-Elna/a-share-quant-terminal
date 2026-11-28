"""策略条件树、策略求值与逐条依据。

由原 ``app/strategy/strategies.py`` 拆分而来；原模块保留为门面，
所有既有 ``from app.strategy.strategies import ...`` 导入保持可用。
"""

from __future__ import annotations
import logging

from dataclasses import dataclass
import pandas as pd
from app.core.errors import UnknownAlgoError
from app.strategy import condition as cond_dsl

logger = logging.getLogger(__name__)

from app.strategy.specs import *  # noqa: F401,F403
from app.strategy.data import *  # noqa: F401,F403
from app.strategy.market import *  # noqa: F401,F403

def _c6_market_detail(df: pd.DataFrame) -> cond_dsl.ConditionDetail:
    """C6 的市场环境要求（scope=market，单独展示）。"""
    market = df.attrs.get("market") or {}
    flag = _market_flag(df, "c6_ok")
    detail_text = (
        "指数数据缺失，按放行处理"
        if market.get("available") is False
        else f"沪深300 close={market.get('close')} ma20={market.get('ma20')}"
    )
    passed = pd.Series(bool(flag), index=df.index, dtype=bool)
    return cond_dsl.ConditionDetail(
        key="c6_market",
        label="沪深300 收盘 ≥ MA20 × 0.995",
        scope="market",
        required=True,
        op=None,
        passed=passed,
        actual=passed,
        detail_text=detail_text,
    )

def _c1_market_detail(df: pd.DataFrame, params: dict) -> cond_dsl.ConditionDetail:
    """C1 的市场环境要求（scope=market）：温度上限 + 非避险下跌。"""
    market = df.attrs.get("market") or {}
    temp_max = float(params.get("temp_max", 70.0))
    temp_available = bool(market.get("temp_available", False))
    regime_available = bool(market.get("regime_available", False))
    temp_ok = _market_temp_ok(market, temp_max)
    regime_ok = _regime_not_risk_off(market)
    temp_text = (
        "温度数据缺失，按放行处理"
        if not temp_available
        else f"温度={market.get('temp_score')}（上限 {temp_max:g}）"
    )
    regime_text = (
        "阶段数据缺失，按放行处理"
        if not regime_available
        else f"regime={market.get('regime_code')}"
    )
    passed = pd.Series(bool(temp_ok and regime_ok), index=df.index, dtype=bool)
    return cond_dsl.ConditionDetail(
        key="c1_market",
        label=f"市场温度 < {temp_max:g} 且市场阶段不是避险下跌",
        scope="market",
        required=True,
        op=None,
        passed=passed,
        actual=passed,
        threshold=temp_max,
        detail_text=f"{temp_text}；{regime_text}",
    )


def _c3_market_detail(df: pd.DataFrame, params: dict) -> cond_dsl.ConditionDetail:
    """C3 的市场环境要求（scope=market）：双均线 + 连续站稳 + MA20 上行。"""
    market = df.attrs.get("market") or {}
    confirm_days = max(1, int(params.get("confirm_days", 2)))
    slope_days = max(1, int(params.get("ma20_slope_days", 5)))
    base_ok = True if market.get("available") is False else bool(market.get("c3_ok", True))
    confirm_ok = _ma20_confirm_ok(market, confirm_days)
    slope_ok = _ma20_slope_ok(market, slope_days)
    detail_text = (
        "指数数据缺失，按放行处理"
        if market.get("available") is False
        else (
            f"沪深300 close={market.get('close')} ma20={market.get('ma20')} "
            f"ma60={market.get('ma60')}；连续站稳={confirm_ok}"
            f"（{confirm_days} 日）；MA20 上行={slope_ok}（{slope_days} 日）"
        )
    )
    passed = pd.Series(
        bool(base_ok and confirm_ok and slope_ok),
        index=df.index,
        dtype=bool,
    )
    return cond_dsl.ConditionDetail(
        key="c3_market",
        label=(
            f"沪深300 ≥ MA20×0.995 且 ≥ MA60；连续 {confirm_days} 日站稳、"
            f"MA20 较 {slope_days} 日前抬升"
        ),
        scope="market",
        required=True,
        op=None,
        passed=passed,
        actual=passed,
        threshold=None,
        detail_text=detail_text,
    )



def _a4_regime_detail(df: pd.DataFrame) -> cond_dsl.ConditionDetail:
    """A4 防御型市场阶段要求（scope=market，单独展示）。"""
    market = df.attrs.get("market") or {}
    regime_available = bool(market.get("regime_available", False))
    regime_code = market.get("regime_code")
    passed_flag = (
        True if not regime_available else regime_code in A4_DEFENSIVE_REGIMES
    )
    detail_text = (
        "市场阶段数据缺失，按放行处理"
        if not regime_available
        else (
            f"regime={regime_code} label={market.get('regime_label')} "
            f"days={market.get('regime_days')} since={market.get('regime_since')}"
        )
    )
    passed = pd.Series(bool(passed_flag), index=df.index, dtype=bool)
    return cond_dsl.ConditionDetail(
        key="a4_regime",
        label="仅在防御型市场阶段启用（震荡筑底/向下调整/避险下跌）",
        scope="market",
        required=True,
        op=None,
        passed=passed,
        actual=passed,
        threshold=sorted(A4_DEFENSIVE_REGIMES),
        detail_text=detail_text,
    )

@dataclass
class StrategyEvaluation:
    """一次策略求值：整体掩码 + 逐条明细 + 实际求值用的 frame。"""

    mask: pd.Series
    details: list[cond_dsl.ConditionDetail]
    frame: pd.DataFrame

def _evaluate_strategy(
    algo_code: str, df: pd.DataFrame, params: dict
) -> StrategyEvaluation:
    """策略求值（DI 条件树 + 市场门槛 + A3/A4 专用口径）。"""
    spec = get_spec(algo_code)
    code = spec.code
    normalized = _validate_params(code, params)
    frame = df

    if code == "a3":
        composite = _numeric_column(frame, "composite_score")
        if "ownership" in frame.columns:
            ownership = frame["ownership"].astype("object")
            central = ownership.eq("央企").fillna(False)
        else:
            central = pd.Series(False, index=frame.index, dtype=bool)
        mask = central & composite.notna() & (composite >= float(normalized["min_score"]))
    elif code == "a4":
        pe = _numeric_column(frame, "pe_ttm")
        dividend = _numeric_column(frame, "dv_ttm")
        pb = _numeric_column(frame, "pb")
        score = _numeric_column(frame, "composite_score")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        ma120 = _numeric_column(frame, "ma120")
        close = _numeric_column(frame, "close")
        mask = (
            (pe > float(normalized["pe_min"]))
            & (pe < float(normalized["pe_max"]))
            & dividend.notna()
            & (dividend >= float(normalized["div_min"]))
            & (dividend <= float(normalized["dv_max"]))
            & (pb > 0)
            & (pb <= float(normalized["pb_max"]))
            & (score >= float(normalized["min_score"]))
            & (amount_ma20 >= float(normalized["min_amount"]))
            & (ma120 > 0)
            & (close >= ma120 * float(normalized["trend_dev"]))
        )
        market = frame.attrs.get("market") or {}
        regime_available = bool(market.get("regime_available", False))
        regime_code = market.get("regime_code")
        if regime_available and regime_code not in A4_DEFENSIVE_REGIMES:
            logger.info("A4 防御型市场门槛未通过 regime=%s，本次不产生信号", regime_code)
            mask = mask & False
        return StrategyEvaluation(
            mask=mask.fillna(False).astype(bool),
            details=[_a4_regime_detail(frame)],
            frame=frame,
        )
    elif code == "c1":
        high_prev = _numeric_column(frame, "high20_prev")
        adj_close = _numeric_column(frame, "adj_close")
        adj_open = _numeric_column(frame, "adj_open")
        prev_close = _numeric_column(frame, "prev_adj_close")
        amount = _numeric_column(frame, "amount")
        mom60 = _numeric_column(frame, "mom60")
        vol20 = _numeric_column(frame, "vol20")
        roc20 = _numeric_column(frame, "roc20")
        limit_up = (
            _to_bool_series(frame["is_limit_up"])
            if "is_limit_up" in frame.columns
            else pd.Series(False, index=frame.index, dtype=bool)
        )
        mask = (
            (adj_close > high_prev)
            & (amount >= float(normalized["min_amount"]))
            & (adj_close > adj_open)
            & (adj_close > prev_close)
            & mom60.notna()
            & (mom60 >= float(normalized["mom60_min"]))
            & vol20.notna()
            & (vol20 <= float(normalized["vol_cap"]))
            # 不追已经加速的突破：20 日涨幅超过上限的标的剔除
            & roc20.notna()
            & (roc20 <= float(normalized["roc20_max"]))
            & ~limit_up
        )
        market = frame.attrs.get("market") or {}
        if not (
            _market_temp_ok(market, normalized["temp_max"])
            and _regime_not_risk_off(market)
        ):
            logger.info(
                "C1 市场温度/阶段门槛未通过 temp=%s regime=%s，本次不产生信号",
                market.get("temp_score"), market.get("regime_code"),
            )
            mask = mask & False
        return StrategyEvaluation(
            mask=mask.fillna(False).astype(bool),
            details=[_c1_market_detail(frame, normalized)],
            frame=frame,
        )
    elif code == "c2":
        range40 = _numeric_column(frame, "range40_ratio")
        high40 = _numeric_column(frame, "high40")
        low10 = _numeric_column(frame, "low10")
        high10 = _numeric_column(frame, "high10")
        volume = _numeric_column(frame, "volume")
        vol_ma20_prev = _numeric_column(frame, "vol_ma20_prev")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        tight_ratio = high10 / low10.where(low10 > 0)
        mask = (
            (range40 > float(normalized["mom_min"]))
            & (tight_ratio < float(normalized["tight_max"]))
            & (low10 >= float(normalized["high_level"]) * high40)
            & (volume < float(normalized["shrink_max"]) * vol_ma20_prev)
            & (amount_ma20 >= float(normalized["min_amount"]))
        )
    elif code == "c3":
        rps = _numeric_column(frame, "rps")
        adj_close = _numeric_column(frame, "adj_close")
        high120 = _numeric_column(frame, "high120")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        roc20 = _numeric_column(frame, "roc20")
        vol20 = _numeric_column(frame, "vol20")
        mask = (
            (rps >= float(normalized["rps_threshold"]))
            & (adj_close >= float(normalized["near_high_ratio"]) * high120)
            & roc20.notna()
            & (roc20 <= float(normalized["roc20_max"]))
            & vol20.notna()
            & (vol20 <= float(normalized["vol_cap"]))
            & (amount_ma20 >= float(normalized["min_amount"]))
        )
        market = frame.attrs.get("market") or {}
        gate_ok = (
            market.get("available") is False
            or bool(market.get("c3_ok", True))
        )
        if not (
            gate_ok
            and _ma20_confirm_ok(market, normalized["confirm_days"])
            and _ma20_slope_ok(market, normalized["ma20_slope_days"])
        ):
            logger.info(
                "C3 市场环境门槛未通过 flags=%s，本次不产生信号",
                market.get("ma20_above_flags"),
            )
            mask = mask & False
        return StrategyEvaluation(
            mask=mask.fillna(False).astype(bool),
            details=[_c3_market_detail(frame, normalized)],
            frame=frame,
        )
    elif code == "c6":
        band_pct = _numeric_column(frame, "boll_width_pct60")
        close = _numeric_column(frame, "close")
        upper = _numeric_column(frame, "boll_upper")
        high20 = _numeric_column(frame, "high20_close")
        volume = _numeric_column(frame, "volume")
        vol_ma5 = _numeric_column(frame, "vol_ma5")
        range20 = _numeric_column(frame, "range20_pct")
        amount_ma20 = _numeric_column(frame, "amount_ma20")
        mask = (
            band_pct.notna()
            & (band_pct <= float(normalized["squeeze_pct"]))
            & (close > upper)
            & (close > high20)
            & (volume >= float(normalized["vol_mult"]) * vol_ma5)
            & (range20.notna())
            & (range20 < float(normalized["range_max"]))
            & (amount_ma20 >= float(normalized["min_amount"]))
        )
        market = frame.attrs.get("market") or {}
        if market.get("available") is not False and not market.get("c6_ok", True):
            logger.info("C6 市场环境门槛未通过，本次不产生信号")
            mask = mask & False
        return StrategyEvaluation(
            mask=mask.fillna(False).astype(bool),
            details=[_c6_market_detail(frame)],
            frame=frame,
        )
    else:  # pragma: no cover - get_spec 已保证策略存在
        raise UnknownAlgoError(f"未知策略: {algo_code}")
    return StrategyEvaluation(
        mask=mask.fillna(False).astype(bool), details=[], frame=frame
    )

def strategy_mask(algo_code: str, df: pd.DataFrame, params: dict) -> pd.Series:
    """返回策略条件命中的布尔 Series（A3/A4/C1/C2/C3/C6）。"""
    return _evaluate_strategy(algo_code, df, params).mask

def filter_by_strategy(algo_code: str, df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """按策略条件过滤候选池（纯函数）。"""
    if df is None or len(df) == 0:
        empty = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        if isinstance(df, pd.DataFrame):
            empty.attrs.update(dict(df.attrs))
        return empty
    mask = strategy_mask(algo_code, df, params)
    filtered = df.loc[mask].copy()
    filtered.attrs.update(dict(df.attrs))
    return filtered

__all__ = [
    "_c1_market_detail",
    "_c3_market_detail",
    "_c6_market_detail",
    "_a4_regime_detail",
    "StrategyEvaluation",
    "_evaluate_strategy",
    "strategy_mask",
    "filter_by_strategy",
]
