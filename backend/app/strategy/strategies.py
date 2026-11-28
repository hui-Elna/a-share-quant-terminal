"""
内置策略（A3/A4 价值守正 + C1/C2/C3/C6 正式启用）与候选池构建
========================================================
**一个策略 = 一个候选池构建步骤 + 一个排序评分步骤**：

  - `build_candidates(algo_code, trade_date, params, db)`：
      一条 SQL 横截面取数（`fact_stock_daily` + `fact_feature_daily` +
      `fact_stock_score_daily` + `fact_moneyflow` + `dim_stock`，全部 LEFT JOIN），
      先在 SQL 里做基础可用性过滤，再在 pandas 里做统一的 A 股口径过滤与策略条件。
      **禁止 N+1 查询**：候选池只查一次库，一次取回当日全市场。
      A4/C6 额外读取沪深300 指数状态（`market_indices`），把市场门槛放进 `frame.attrs["market"]`。
  - `rank_signals(algo_code, df, params)`：纯函数（无 DB），把候选池按策略口径再筛一次、
      排序、截断到 `limit`，并生成 `rank / score / metrics`，供 API 直接序列化。
      其中 **score = 本策略排序指标的原始值**。

当前正式启用 A3/A4/C1/C2/C3/C6（2026-11-28 起四个 C 系列由观察候选转为正式启用；
A1/A2/B1–B5 的实现已删除，历史 run 仍可查询；C4/C5 已于 2026-11-22 移除）：
  A3 央企 × 综合评分
      ``dim_stock.ownership == '央企'`` 且 ``composite_score >= min_score(默认6)``，
      按 ``composite_score`` 降序。
  A4 高股息质量 × 低估值
      ``pe_ttm > 0``、``pe_ttm < pe_max``、``div_min <= dv_ttm <= dv_max``、
      ``0 < pb <= pb_max``、``composite_score >= min_score``、20 日均额达标、
      ``close >= MA120 × trend_dev``，且仅在防御型市场阶段
      （bottoming/pull_down/risk_off）启用，按 ``dv_ttm`` 降序。
  C1 海龟突破：20 日新高 + 成交额过亿 + 阳线真涨，按流通市值降序。
  C2 高窄旗形：40 日强动量后 10 日极度收敛缩量，按 40 日区间比降序。
  C3 RPS 相对强度突破：120 日 RPS 前 10% 且接近 120 日新高，按 RPS 降序。
  C4 涨停洗盘、C5 均线放量突破已于 2026-11-22 永久移除（历史回测无正向边际）。
  C6 布林挤压突破：带宽低分位 + 突破上轨新高 + 放量，按带宽分位升序。
  C6 布林挤压突破：带宽低分位 + 突破上轨新高 + 放量，按带宽分位升序。

统一前置过滤（所有策略一致）：
  剔除 ST、status != 1（停牌/退市）、delist_date 已到、上市不足 60 个交易日、
  is_suspended、当日无行情（无 fact_stock_daily 行）。
"""

from app.strategy.specs import *  # noqa: F401,F403
from app.strategy.data import *  # noqa: F401,F403
from app.strategy.market import *  # noqa: F401,F403
from app.strategy.conditions import *  # noqa: F401,F403
from app.strategy.ranking import *  # noqa: F401,F403
from app.strategy.candidates import *  # noqa: F401,F403

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
    "_DIM_COLUMNS",
    "_DAILY_COLUMNS",
    "_FEATURE_COLUMNS",
    "_SCORE_COLUMNS",
    "_FLOW_COLUMNS",
    "_NUMERIC_COLUMNS",
    "_BOOL_COLUMNS",
    "_decimal_to_float",
    "_build_record",
    "_fetch_cross_section",
    "_WINDOW_STRATEGIES",
    "_DERIVED_COLUMNS",
    "_window_features",
    "_enrich_history",
    "_to_float_series",
    "coerce_numeric_frame",
    "_to_float_quiet",
    "_to_bool_quiet",
    "_to_bool_series",
    "_normalize_dtypes",
    "_load_open_dates",
    "listed_trading_days",
    "apply_common_filters",
    "_numeric_column",
    "_MIN_FEATURE_COVERAGE",
    "_assert_data_ready",
    "load_market_context",
    "market_gate_payload",
    "_market_flag",
    "_market_temp_ok",
    "_regime_not_risk_off",
    "_ma20_confirm_ok",
    "_ma20_slope_ok",
    "_c1_market_detail",
    "_c3_market_detail",
    "_c6_market_detail",
    "_a4_regime_detail",
    "StrategyEvaluation",
    "_evaluate_strategy",
    "strategy_mask",
    "filter_by_strategy",
    "_sort_candidates",
    "_score_series",
    "_FACTOR_META",
    "_score_breakdown",
    "_metrics_for_row",
    "_empty_signal_frame",
    "rank_signals",
    "build_candidates",
]
