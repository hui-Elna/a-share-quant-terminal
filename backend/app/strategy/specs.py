"""策略元信息、参数校验与策略注册表。

由原 ``app/strategy/strategies.py`` 拆分而来；原模块保留为门面，
所有既有 ``from app.strategy.strategies import ...`` 导入保持可用。
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
import numpy as np
import pandas as pd
from app.core.config import STRATEGY_DEFAULTS
from app.core.errors import InvalidParamError, UnknownAlgoError

#: 次新股过滤口径（用户定稿：上市不足 60 个交易日不入选）
NEW_STOCK_MIN_TRADING_DAYS = 60

#: 无交易日历时的近似：60 个交易日 ≈ 84 个自然日（60 × 7 / 5）
_FALLBACK_NEW_STOCK_CALENDAR_DAYS = 84

#: 市场状态门槛使用的基准指数
BENCHMARK_INDEX_CODE = "sh000300"

#: A4 防御型市场门槛：只在震荡筑底/向下调整/避险下跌阶段启用
A4_DEFENSIVE_REGIMES = frozenset({"bottoming", "pull_down", "risk_off"})

#: 前端展示用"通用字段"（契约 5.2 data[] 里的顶层列）
DISPLAY_FIELDS: tuple[str, ...] = (
    "close", "pct_chg", "pe_ttm", "pb", "ps_ttm", "dv_ttm",
    "macd_dif", "macd_dea", "rsi14", "composite_score",
)

def _param(
    key: str,
    label: str,
    default: Any,
    minimum: float,
    maximum: float,
    kind: str = "number",
) -> dict[str, Any]:
    """构造一个参数 schema 项（契约 5.1 的 key/label/type/default/min/max）。"""
    return {
        "key": key,
        "label": label,
        "type": kind,
        "default": default,
        "min": minimum,
        "max": maximum,
    }

@dataclass(frozen=True)
class StrategySpec:
    """策略元信息。`defaults` 里是**归一化后**的参数默认值（含 limit）。"""

    code: str
    name: str
    params_schema: list[dict]
    defaults: dict
    algo_type: str = "technical"
    short_desc: str = ""
    full_desc: str = ""
    sort_order: int = 0

    def as_algo_item(
        self, *, is_active: bool = True, is_observation: bool = False
    ) -> dict:
        """转成契约 5.1 的算法清单项。"""
        return {
            "algo_code": self.code,
            "algo_name": self.name,
            "algo_type": self.algo_type,
            "short_desc": self.short_desc,
            "full_desc": self.full_desc,
            "params_schema": [dict(item) for item in self.params_schema],
            "is_active": is_active,
            "is_observation": is_observation,
            "sort_order": self.sort_order,
        }

SPECS: dict[str, StrategySpec] = {
    "a3": StrategySpec(
        code="a3",
        name="央企 × 综合评分TOP",
        algo_type="score",
        sort_order=1,
        short_desc="央企中综合评分达标者，按综合评分降序",
        full_desc=(
            "在 dim_stock.ownership = '央企' 的标的中，保留综合评分 >= min_score 者，"
            "按 composite_score 降序取前 limit 只。"
        ),
        defaults={**dict(STRATEGY_DEFAULTS["a3"]), "min_score": 6.0},  # 0–10 分制，自洽兜底
        params_schema=[
            _param("limit", "返回数量上限", 20, 1, 1000, kind="integer"),
            _param("min_score", "最低综合评分(0-10)", 6.0, 0.0, 10.0),
        ],
    ),
    "a4": StrategySpec(
        code="a4",
        name="高股息质量 × 低估值",
        algo_type="value",
        sort_order=2,
        short_desc="PE/PB 受控、股息率达标、质量与趋势过滤后，按股息率降序",
        full_desc=(
            "剔除 PE<=pe_min、PE>=pe_max、PB<=0；保留股息率在 [div_min, dv_max]、"
            "PB <= pb_max、综合评分 >= min_score、20 日均成交额 >= min_amount、"
            "close >= MA120 × trend_dev 者，按股息率降序取前 limit 只。"
            "pe_min 用于剔除「低 PE 陷阱」（周期股/一次性收益），"
            "min_score 6.5 用于剔除高股息但质量差的标的。"
        ),
        defaults=dict(STRATEGY_DEFAULTS["a4"]),
        params_schema=[
            _param("limit", "返回数量上限", 20, 1, 1000, kind="integer"),
            _param("pe_min", "PE_TTM 下限", 3.0, 0.0, 1000.0),
            _param("pe_max", "PE_TTM 上限", 15.0, 0.01, 1000.0),
            _param("div_min", "股息率下限(%)", 4.5, 0.0, 100.0),
            _param("dv_max", "股息率上限(%)", 15.0, 0.1, 100.0),
            _param("pb_max", "PB 上限", 2.5, 0.1, 50.0),
            _param("min_score", "最低综合评分(0-10)", 6.5, 0.0, 10.0),
            _param("min_amount", "20日均额下限(元)", 5.0e7, 0.0, 1.0e12),
            _param("trend_dev", "MA120 趋势容忍度", 0.9, 0.5, 1.5),
        ],
    ),
    "c1": StrategySpec(
        code="c1",
        name="海龟突破",
        algo_type="breakout",
        sort_order=3,
        short_desc="20 日新高 + 中期动量为正 + 低波动 + 成交额过亿，按流通市值降序",
        full_desc=(
            "前 lookback（默认 20）个交易日最高价被今日收盘突破；今日成交额 >= min_amount；"
            "今日收阳（close > open）且真涨（close > prev_close）；"
            "60 日动量 >= mom60_min（默认 0，剔除下跌趋势中的假突破）；"
            "20 日收益波动率（日标准差）<= vol_cap（默认 0.05，剔除高波动题材股）；"
            "当日涨停不入选（T+1 开盘难以成交）；按流通市值降序取前 limit 只。"
        ),
        defaults=dict(STRATEGY_DEFAULTS["c1"]),
        params_schema=[
            _param("limit", "返回数量上限", 20, 1, 1000, kind="integer"),
            _param("lookback", "突破窗口(日)", 20, 5, 60, kind="integer"),
            _param("min_amount", "当日成交额下限(元)", 1.0e8, 0.0, 1.0e12),
            _param("mom60_min", "60日动量下限", 0.0, -1.0, 1.0),
            _param("vol_cap", "20日波动率上限", 0.05, 0.005, 0.5),
        ],
    ),
    "c2": StrategySpec(
        code="c2",
        name="高窄旗形",
        algo_type="breakout",
        sort_order=4,
        short_desc="40 日强动量后 10 日极度收敛缩量，按 40 日区间比降序",
        full_desc=(
            "40 日最高/最低 > mom_min（默认 1.6）；最近 10 日最高/最低 < tight_max（默认 1.12）；"
            "10 日最低 >= high_level × 40 日最高（默认 0.8）；今日成交量 < shrink_max（默认 0.5）"
            "× 前 20 日均量；20 日均额 >= min_amount（默认 1 亿）；按 40 日区间比降序取前 limit 只。"
            "收敛与缩量阈值比旧版更严（1.15→1.12、0.6→0.5），换取更高的形态成功率。"
        ),
        defaults=dict(STRATEGY_DEFAULTS["c2"]),
        params_schema=[
            _param("limit", "返回数量上限", 20, 1, 1000, kind="integer"),
            _param("lookback", "动量窗口(日)", 40, 20, 80, kind="integer"),
            _param("mom_min", "40日区间比下限", 1.6, 1.0, 5.0),
            _param("tight_max", "10日收敛上限", 1.12, 1.0, 2.0),
            _param("high_level", "高位保持比例", 0.8, 0.5, 1.0),
            _param("shrink_max", "缩量比例上限", 0.5, 0.1, 1.0),
            _param("min_amount", "20日均额下限(元)", 1.0e8, 0.0, 1.0e12),
        ],
    ),
    "c3": StrategySpec(
        code="c3",
        name="RPS 相对强度突破",
        algo_type="breakout",
        sort_order=5,
        short_desc="120 日相对强度前 10% + 接近新高 + 短期未过热 + 低波动，按 RPS 降序",
        full_desc=(
            "lookback（默认 120）日收益率在合格股票池内的横截面百分位 >= rps_threshold（默认 90）；"
            "收盘 >= near_high_ratio（默认 0.85）× lookback 日最高；"
            "20 日涨幅 <= roc20_max（默认 0.08，剔除已加速的追高标的）；"
            "20 日收益波动率（日标准差）<= vol_cap（默认 0.05）；"
            "20 日均额达标；沪深300 收盘 >= MA20 × 0.995 且 >= MA60 才开仓"
            "（指数缺失按放行）；按 RPS 降序。"
        ),
        defaults=dict(STRATEGY_DEFAULTS["c3"]),
        params_schema=[
            _param("limit", "返回数量上限", 20, 1, 1000, kind="integer"),
            _param("lookback", "RPS 窗口(日)", 120, 60, 250, kind="integer"),
            _param("rps_threshold", "RPS 下限(分位)", 90.0, 50.0, 100.0),
            _param("near_high_ratio", "接近新高比例", 0.85, 0.5, 1.0),
            _param("min_amount", "20日均额下限(元)", 1.0e8, 0.0, 1.0e12),
            _param("vol_cap", "20日波动率上限", 0.05, 0.005, 0.5),
            _param("roc20_max", "20日涨幅上限", 0.08, 0.0, 1.0),
        ],
    ),
    "c6": StrategySpec(
        code="c6",
        name="布林挤压突破",
        algo_type="breakout",
        sort_order=8,
        short_desc="布林带宽低分位 + 突破上轨新高 + 放量，按带宽分位升序",
        full_desc=(
            "布林带宽处于 60 日最低 squeeze_pct（默认 20）分位；收盘突破上轨且创 20 日新高；"
            "成交量 >= 5 日均量 × vol_mult；20 日区间宽度 < range_max；20 日均额达标；"
            "沪深300 收盘 >= MA20 × 0.995；按带宽分位升序。"
        ),
        defaults=dict(STRATEGY_DEFAULTS["c6"]),
        params_schema=[
            _param("limit", "返回数量上限", 20, 1, 1000, kind="integer"),
            _param("squeeze_pct", "带宽分位上限(%)", 20.0, 1.0, 100.0),
            _param("vol_mult", "突破量能倍数", 1.5, 0.1, 10.0),
            _param("range_max", "20日区间宽度上限(%)", 10.0, 0.0, 100.0),
            _param("min_amount", "20日均额下限(元)", 2.0e8, 0.0, 1.0e12),
        ],
    ),
}

#: 观察候选池：当前为空。C1/C2/C3/C6 已于 2026-11-28 并入正式启用集（SPECS），
#: 保留该注册表供后续新策略「先回测、通过门槛后再启用」。
CANDIDATE_SPECS: dict[str, StrategySpec] = {}

#: 排序口径： (字段, 是否升序)
_SORT_SPEC: dict[str, tuple[str, bool]] = {
    "a3": ("composite_score", False),      # 综合评分降序
    "a4": ("dv_ttm", False),               # 股息率降序
    "c1": ("float_cap", False),            # 流通市值降序
    "c2": ("range40_ratio", False),        # 40日区间比降序
    "c3": ("rps", False),                  # RPS 降序
    "c6": ("boll_width_pct60", True),      # 带宽分位升序
}

#: 各策略 metrics 字段（该策略依据的全部指标；前端表格列由它动态生成）
_METRIC_FIELDS: dict[str, tuple[str, ...]] = {
    "a3": (
        "composite_score", "value_score", "quality_score", "momentum_score",
        "volatility_score", "liquidity_score", "sentiment_score",
        "ownership", "industry_l1", "close", "pct_chg", "pe_ttm", "pb",
    ),
    "a4": (
        "pe_ttm", "dv_ttm", "pb", "ps_ttm", "composite_score",
        "amount_ma20", "ma120", "close", "pct_chg", "industry_l1",
    ),
    "c1": (
        "high20_prev", "adj_close", "adj_open", "prev_adj_close", "mom60", "vol20",
        "amount", "float_cap", "close", "pct_chg", "industry_l1",
    ),
    "c2": (
        "range40_ratio", "high40", "low40", "high10", "low10",
        "vol_ma20_prev", "volume", "close", "pct_chg", "amount_ma20", "industry_l1",
    ),
    "c3": (
        "rps", "roc120", "roc20", "vol20", "high120", "adj_close", "close", "pct_chg",
        "amount_ma20", "industry_l1",
    ),
    "c6": (
        "boll_width_pct60", "boll_upper", "high20_close", "range20_pct",
        "volume", "vol_ma5", "amount_ma20", "close", "pct_chg", "industry_l1",
    ),
}

def jsonable(value: Any) -> Any:
    """把 numpy / Decimal / NaN / date 等转成可 JSON 序列化的原生对象。"""
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    if isinstance(value, Decimal):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return [jsonable(item) for item in value.tolist()]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)

def get_spec(algo_code: str) -> StrategySpec:
    """按代码取策略元信息；未知策略抛 UnknownAlgoError。"""
    key = str(algo_code or "").strip().lower()
    spec = SPECS.get(key) or CANDIDATE_SPECS.get(key)
    if spec is None:
        raise UnknownAlgoError(
            f"未知策略: {algo_code}",
            detail={"available": sorted(SPECS)},
        )
    return spec

def list_specs() -> list[StrategySpec]:
    """全部策略元信息（按 sort_order 排序）。"""
    return sorted(SPECS.values(), key=lambda item: item.sort_order)

def describe_specs() -> list[dict]:
    """转成契约 5.1 的算法清单（供 `GET /api/v1/algo/list` 直接用）。"""
    return [spec.as_algo_item() for spec in list_specs()]

def _check_range(
    key: str,
    value: float,
    minimum: Any,
    maximum: Any,
) -> None:
    """数值越界校验。"""
    if minimum is not None and value < float(minimum) - 1e-12:
        raise InvalidParamError(
            f"参数 {key} 不能小于 {minimum}，收到: {value}",
            detail={"param": key, "value": value, "min": minimum, "max": maximum},
        )
    if maximum is not None and value > float(maximum) + 1e-12:
        raise InvalidParamError(
            f"参数 {key} 不能大于 {maximum}，收到: {value}",
            detail={"param": key, "value": value, "min": minimum, "max": maximum},
        )

def _validate_params(algo_code: str, params: dict | None) -> dict:
    """参数白名单 + 类型转换 + 默认值补齐（**参数真生效的第一道关**）。

    - 未传的参数从 `SPECS[algo].defaults` 补齐；
    - 参数类型按 schema 归一（limit → int，其余 → float），保证缓存指纹稳定；
    - A3 的 `min_score` 是 0–10 分制：**兼容旧调用方传 0–100 分值**（如 60 → 6.0），
      归一后的值会回显在 run.params 里，避免"静默空结果"；
    - 未知参数 / 越界 / 非数值 → `InvalidParamError`。
    """
    spec = get_spec(algo_code)
    raw = dict(params or {})
    allowed = {item["key"] for item in spec.params_schema}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise InvalidParamError(
            f"策略 {spec.code} 不支持参数: {', '.join(unknown)}",
            detail={"algo": spec.code, "unknown": unknown, "allowed": sorted(allowed)},
        )

    normalized: dict[str, Any] = {}
    for item in spec.params_schema:
        key = item["key"]
        value = raw.get(key, spec.defaults.get(key, item.get("default")))
        if value is None:
            raise InvalidParamError(
                f"参数 {key} 不能为空", detail={"algo": spec.code, "param": key}
            )
        if item.get("type") == "integer":
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise InvalidParamError(
                    f"参数 {key} 必须是整数，收到: {value!r}",
                    detail={"algo": spec.code, "param": key, "value": str(value)},
                ) from None
            if isinstance(value, float) and not float(value).is_integer():
                raise InvalidParamError(
                    f"参数 {key} 必须是整数，收到: {value!r}",
                    detail={"algo": spec.code, "param": key, "value": str(value)},
                )
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise InvalidParamError(
                    f"参数 {key} 必须是数值，收到: {value!r}",
                    detail={"algo": spec.code, "param": key, "value": str(value)},
                ) from None
            if math.isnan(number) or math.isinf(number):
                raise InvalidParamError(
                    f"参数 {key} 必须是有限数值，收到: {value!r}",
                    detail={"algo": spec.code, "param": key, "value": str(value)},
                )
            # 旧调用方兼容：A3 最低综合评分传了 0–100 口径（如 60）→ 归一为 0–10（6.0）
            if spec.code == "a3" and key == "min_score" and number > 10.0:
                number = round(number / 10.0, 4)
        _check_range(key, float(number), item.get("min"), item.get("max"))
        normalized[key] = number

    return normalized

#: 公开别名（API 层可先校验再执行，避免重复实现）
validate_params = _validate_params

__all__ = [
    "NEW_STOCK_MIN_TRADING_DAYS",
    "_FALLBACK_NEW_STOCK_CALENDAR_DAYS",
    "BENCHMARK_INDEX_CODE",
    "A4_DEFENSIVE_REGIMES",
    "DISPLAY_FIELDS",
    "_param",
    "StrategySpec",
    "SPECS",
    "CANDIDATE_SPECS",
    "_SORT_SPEC",
    "_METRIC_FIELDS",
    "jsonable",
    "get_spec",
    "list_specs",
    "describe_specs",
    "_check_range",
    "_validate_params",
    "validate_params",
]
