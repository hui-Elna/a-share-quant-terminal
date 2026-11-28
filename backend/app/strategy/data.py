"""策略横截面取数、窗口特征与统一过滤。

由原 ``app/strategy/strategies.py`` 拆分而来；原模块保留为门面，
所有既有 ``from app.strategy.strategies import ...`` 导入保持可用。
"""

from __future__ import annotations
import logging

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Sequence
import numpy as np
import pandas as pd
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import HISTORY_START
from app.core.errors import DataUnavailableError
from app.db.models.dim import DimStock
from app.db.models.fact import (
    FactFeatureDaily,
    FactMoneyflow,
    FactStockDaily,
    FactStockScoreDaily,
)
from app.db.models.meta import TradeCalendar

logger = logging.getLogger(__name__)

from app.strategy.specs import *  # noqa: F401,F403

#: 各表需要取回的列（显式声明，避免 SELECT * 带来的列名冲突/浪费）
_DIM_COLUMNS = (
    "code", "name", "exchange", "board",
    "industry_l1", "industry_l2", "ownership",
    "list_date", "delist_date", "is_st", "status",
)

_DAILY_COLUMNS = (
    "open", "high", "low", "close", "pre_close",
    "pct_chg", "volume", "amount", "turnover_rate", "adj_factor",
    "market_cap", "float_cap", "pe_ttm", "pb", "ps_ttm", "dv_ttm",
    "is_limit_up", "is_limit_down", "is_suspended",
)

_FEATURE_COLUMNS = (
    "ma5", "ma10", "ma20", "ma60", "ma120", "rsi14", "rsi6", "roc10",
    "macd_dif", "macd_dea", "macd_hist", "macd_golden_cross",
    "ema5", "ema20", "ema60", "ema20_up", "ema_golden_cross", "ema_cross_3d",
    "kdj_k", "kdj_d", "kdj_j", "kdj_j_min2", "kdj_golden_cross",
    "boll_mid", "boll_upper", "boll_lower", "boll_width", "boll_width_pct60",
    "high20_close", "range20_pct", "amp60", "vol_ma5",
    "atr14", "momentum_5", "momentum_20", "momentum_60",
    "volatility_20", "volume_ratio", "amount_ma20", "ma_bullish",
)

_SCORE_COLUMNS = (
    "value_score", "quality_score", "momentum_score",
    "volatility_score", "liquidity_score", "sentiment_score", "composite_score",
    "detail",
)

_FLOW_COLUMNS = (
    "main_net_t0", "main_net_t1", "main_net_t2", "main_net_t3", "main_net_t4",
    "main_net_5d", "north_hold_ratio",
)

_NUMERIC_COLUMNS: tuple[str, ...] = (
    "market_cap", "float_cap", "turnover_rate", "pe_ttm", "pb", "ps_ttm", "dv_ttm",
    "ma5", "ma10", "ma20", "ma60", "ma120", "rsi14", "rsi6", "roc10",
    "macd_dif", "macd_dea", "macd_hist",
    "ema5", "ema20", "ema60",
    "kdj_k", "kdj_d", "kdj_j", "kdj_j_min2",
    "boll_mid", "boll_upper", "boll_lower", "boll_width", "boll_width_pct60",
    "high20_close", "range20_pct", "amp60", "vol_ma5",
    "atr14",
    "momentum_5", "momentum_20", "momentum_60", "volatility_20",
    "volume_ratio", "amount_ma20",
    "value_score", "quality_score", "momentum_score",
    "volatility_score", "liquidity_score", "sentiment_score", "composite_score",
    "main_net_t0", "main_net_t1", "main_net_t2", "main_net_t3", "main_net_t4",
    "main_net_5d", "north_hold_ratio",
) + _DAILY_COLUMNS

_BOOL_COLUMNS: tuple[str, ...] = (
    "is_st", "is_suspended", "is_limit_up", "is_limit_down",
    "macd_golden_cross", "ma_bullish",
    "ema20_up", "ema_golden_cross", "ema_cross_3d", "kdj_golden_cross",
)

def _decimal_to_float(value: Any) -> Any:
    """asyncpg 的 Numeric 返回 Decimal，统一转 float 便于 pandas 计算。"""
    if isinstance(value, Decimal):
        return float(value)
    return value

def _build_record(
    daily: FactStockDaily,
    dim: DimStock | None,
    feature: FactFeatureDaily | None,
    score: FactStockScoreDaily | None,
    flow: FactMoneyflow | None,
) -> dict:
    """把一行 ORM 对象拍平成 dict（Decimal → float）。"""
    record: dict[str, Any] = {"trade_date": daily.trade_date}
    for name in _DAILY_COLUMNS:
        record[name] = _decimal_to_float(getattr(daily, name, None))
    for name in _DIM_COLUMNS:
        record[name] = _decimal_to_float(getattr(dim, name, None)) if dim is not None else None
    for name in _FEATURE_COLUMNS:
        record[name] = _decimal_to_float(getattr(feature, name, None)) if feature is not None else None
    for name in _SCORE_COLUMNS:
        record[name] = _decimal_to_float(getattr(score, name, None)) if score is not None else None
    for name in _FLOW_COLUMNS:
        record[name] = _decimal_to_float(getattr(flow, name, None)) if flow is not None else None
    return record

async def _fetch_cross_section(db: AsyncSession, trade_date: date) -> pd.DataFrame:
    """当日全市场横截面：一条 SQL 取回五表 join 结果。

    基础可用性过滤直接下推到 SQL（ST/退市/停牌状态/退市日期），
    其余口径（次新股、当日无行情等）在 pandas 里做，便于纯函数测试。
    """
    daily = FactStockDaily
    statement = (
        select(daily, DimStock, FactFeatureDaily, FactStockScoreDaily, FactMoneyflow)
        .select_from(daily)
        .join(DimStock, DimStock.code == daily.code)
        .outerjoin(
            FactFeatureDaily,
            and_(
                FactFeatureDaily.code == daily.code,
                FactFeatureDaily.trade_date == daily.trade_date,
            ),
        )
        .outerjoin(
            FactStockScoreDaily,
            and_(
                FactStockScoreDaily.code == daily.code,
                FactStockScoreDaily.trade_date == daily.trade_date,
            ),
        )
        .outerjoin(
            FactMoneyflow,
            and_(
                FactMoneyflow.code == daily.code,
                FactMoneyflow.trade_date == daily.trade_date,
            ),
        )
        .where(daily.trade_date == trade_date)
        .where(DimStock.status == 1)
        .where(DimStock.is_st.is_(False))
        .where(or_(DimStock.delist_date.is_(None), DimStock.delist_date > trade_date))
    )

    try:
        result = await db.execute(statement)
        rows = result.all()
    except ProgrammingError as exc:  # 表/字段不存在：多半是数据库未初始化
        raise DataUnavailableError(
            "数据库表尚未初始化，无法读取选股横截面",
            next_action="执行数据库初始化（建表 + 分区 + 视图）后重试",
            detail={"error": str(exc)[:300]},
        ) from exc

    if not rows:
        return pd.DataFrame(columns=list(_DIM_COLUMNS) + list(_DAILY_COLUMNS)
                            + list(_FEATURE_COLUMNS) + list(_SCORE_COLUMNS)
                            + list(_FLOW_COLUMNS) + ["trade_date"])

    records = [_build_record(*row) for row in rows]
    frame = pd.DataFrame.from_records(records)
    _normalize_dtypes(frame)
    frame.attrs.update({"trade_date": trade_date, "source": "db"})

    # 便于排查的规模日志（不打印参数值，避免日志噪音）
    logger.info(
        "选股横截面取数完成 trade_date=%s rows=%d features=%d scores=%d moneyflow=%d",
        trade_date,
        len(frame),
        int(frame["macd_golden_cross"].notna().sum()) if "macd_golden_cross" in frame else 0,
        int(frame["composite_score"].notna().sum()) if "composite_score" in frame else 0,
        int(frame["main_net_5d"].notna().sum()) if "main_net_5d" in frame else 0,
    )
    return frame

#: 需要历史窗口的候选策略
_WINDOW_STRATEGIES = frozenset({"c1", "c2", "c3"})

#: 各候选策略的派生列（用于空窗口时补齐，避免上层 KeyError）
_DERIVED_COLUMNS: dict[str, tuple[str, ...]] = {
    "c1": (
        "high20_prev", "prev_adj_close", "adj_close", "adj_open",
        "mom60", "roc20", "vol20",
    ),
    "c2": ("range40_ratio", "high40", "low40", "high10", "low10", "vol_ma20_prev"),
    "c3": ("roc120", "roc20", "vol20", "high120", "adj_close", "rps"),
}

def _realized_vol(closes: np.ndarray) -> float:
    """区间收益波动率（日标准差，ddof=0，与回测/研究口径一致）。

    ``closes`` 是 N 个收盘价，返回 N-1 个日收益的标准差；出现非正/非有限值时
    返回 NaN，由调用方的 notna 条件自然过滤，不做插补。
    """
    values = np.asarray(closes, dtype=float)
    if values.size < 3 or not np.isfinite(values).all() or (values <= 0).any():
        return float("nan")
    returns = values[1:] / values[:-1] - 1.0
    return float(np.std(returns, ddof=0))


def _window_features(
    bars: pd.DataFrame, algo_code: str, lookback: int
) -> dict[str, Any] | None:
    """单只股票窗口 bars → 派生特征（纯函数；bars 按日期升序，已过滤停牌/零量）。"""
    if len(bars) < 2:
        return None
    adj = (
        pd.to_numeric(bars["adj_factor"], errors="coerce")
        .fillna(1.0)
        .replace(0.0, 1.0)
    )
    adj_high = (pd.to_numeric(bars["high"], errors="coerce") * adj).to_numpy(dtype=float)
    adj_low = (pd.to_numeric(bars["low"], errors="coerce") * adj).to_numpy(dtype=float)
    adj_open = (pd.to_numeric(bars["open"], errors="coerce") * adj).to_numpy(dtype=float)
    adj_close = (pd.to_numeric(bars["close"], errors="coerce") * adj).to_numpy(dtype=float)
    volume = pd.to_numeric(bars["volume"], errors="coerce").to_numpy(dtype=float)

    if algo_code == "c1":
        n = int(lookback)
        # 60 日动量需要 61 根 K 线；突破窗口只在其一
        if len(bars) < max(n + 1, 61):
            return None
        window_high = adj_high[-n - 1:-1]
        if not np.isfinite(window_high).all() or not np.isfinite(adj_close[-61:]).all():
            return None
        if adj_close[-61] <= 0 or adj_close[-21] <= 0:
            return None
        mom60 = float(adj_close[-1] / adj_close[-61] - 1.0)
        roc20 = float(adj_close[-1] / adj_close[-21] - 1.0)
        vol20 = _realized_vol(adj_close[-21:])
        return {
            "high20_prev": float(np.max(window_high)),
            "prev_adj_close": float(adj_close[-2]),
            "adj_close": float(adj_close[-1]),
            "adj_open": float(adj_open[-1]),
            "mom60": mom60,
            "roc20": roc20,
            "vol20": vol20,
        }

    if algo_code == "c2":
        n = int(lookback)
        if len(bars) < max(n, 21):
            return None
        high40 = float(np.max(adj_high[-n:]))
        low40 = float(np.min(adj_low[-n:]))
        high10 = float(np.max(adj_high[-10:]))
        low10 = float(np.min(adj_low[-10:]))
        vol_ma20_prev = float(np.mean(volume[-21:-1]))
        if low40 <= 0 or low10 <= 0 or vol_ma20_prev <= 0:
            return None
        return {
            "range40_ratio": high40 / low40,
            "high40": high40,
            "low40": low40,
            "high10": high10,
            "low10": low10,
            "vol_ma20_prev": vol_ma20_prev,
        }

    if algo_code == "c3":
        n = int(lookback)
        if len(bars) < max(n + 1, 21):
            return None
        start, end = adj_close[-n - 1], adj_close[-1]
        if not np.isfinite(start) or start <= 0 or not np.isfinite(end):
            return None
        high_n = adj_high[-n:]
        if not np.isfinite(high_n).all() or not np.isfinite(adj_close[-21:]).all():
            return None
        if adj_close[-21] <= 0:
            return None
        return {
            "roc120": float(end / start - 1.0),
            "roc20": float(adj_close[-1] / adj_close[-21] - 1.0),
            "vol20": _realized_vol(adj_close[-21:]),
            "high120": float(np.max(high_n)),
            "adj_close": float(end),
        }

    return None

async def _enrich_history(
    frame: pd.DataFrame,
    trade_date: date,
    db: AsyncSession,
    algo_code: str,
    params: dict,
) -> pd.DataFrame:
    """为 c1–c3 一次性加载最近 N 根 K 线并计算派生列（无未来数据）。"""
    if frame is None or len(frame) == 0:
        return frame
    attrs = dict(frame.attrs)
    lookback = int(params.get("lookback", 0) or 0)
    # c1 的 60 日动量需要额外历史：窗口取 max(lookback, 60)，日历天数留足节假日余量
    required = max(lookback, 60) if algo_code == "c1" else max(lookback, 2)
    calendar_days = required * 2 + 30
    start = trade_date - timedelta(days=calendar_days)
    codes = sorted(set(frame["code"].astype(str).str.zfill(6).tolist()))

    statement = (
        select(
            FactStockDaily.code,
            FactStockDaily.trade_date,
            FactStockDaily.open,
            FactStockDaily.high,
            FactStockDaily.low,
            FactStockDaily.close,
            FactStockDaily.volume,
            FactStockDaily.adj_factor,
            FactStockDaily.is_limit_up,
            FactStockDaily.is_suspended,
        )
        .where(FactStockDaily.code.in_(codes))
        .where(FactStockDaily.trade_date >= start)
        .where(FactStockDaily.trade_date <= trade_date)
        .order_by(FactStockDaily.code, FactStockDaily.trade_date)
    )
    rows = (await db.execute(statement)).all()
    if not rows:
        result = frame.copy()
        for column in _DERIVED_COLUMNS[algo_code]:
            result[column] = np.nan
        result.attrs.update(attrs)
        return result

    bars = pd.DataFrame(
        rows,
        columns=[
            "code", "trade_date", "open", "high", "low", "close",
            "volume", "adj_factor", "is_limit_up", "is_suspended",
        ],
    )
    bars["code"] = bars["code"].astype(str).str.zfill(6)
    bars = bars[~bars["is_suspended"].fillna(False).astype(bool)]
    bars = bars[pd.to_numeric(bars["volume"], errors="coerce").fillna(0.0) > 0]
    bars = bars.sort_values(["code", "trade_date"], kind="mergesort")

    records: list[dict[str, Any]] = []
    for code, group in bars.groupby("code", sort=False):
        features = _window_features(group, algo_code, lookback)
        if features is not None:
            records.append({"code": code, **features})

    result = frame.copy()
    if records:
        derived = pd.DataFrame(records)
        result = result.merge(derived, on="code", how="left")
    for column in _DERIVED_COLUMNS[algo_code]:
        if column not in result.columns:
            result[column] = np.nan

    if algo_code == "c3":
        roc = _to_float_series(result["roc120"])
        result["rps"] = roc.rank(pct=True) * 100.0

    result.attrs.update(attrs)
    logger.info(
        "窗口特征完成 algo=%s trade_date=%s codes=%d enriched=%d lookback=%d",
        algo_code, trade_date, len(codes), len(records), lookback,
    )
    return result

def _to_float_series(series: pd.Series) -> pd.Series:
    """列转 float64（Decimal/字符串/None → float/NaN），空列安全。

    asyncpg 读 `Numeric` 列返回 `Decimal`；`Decimal >= float` 会抛 TypeError，
    所以候选池/信号计算的每个数值列都必须先经过这里统一转 float。
    """
    if len(series) == 0:
        return pd.Series(dtype="float64", index=series.index)
    if pd.api.types.is_numeric_dtype(series):
        return series.astype("float64")
    converted = pd.to_numeric(series, errors="coerce")
    if converted.dtype == object:
        # pd.to_numeric 对 Decimal 对象可能不识别，退回逐值 float()
        converted = series.map(_to_float_quiet)
    return converted.astype("float64")

def coerce_numeric_frame(df: pd.DataFrame) -> pd.DataFrame:
    """把候选池 DataFrame 的数值列统一成 float64（Decimal 兜底，可重复调用）。

    方案 D2/D4 硬要求：所有金额/比率比较都在 float 上做，绝不直接比 Decimal。
    """
    if df is None or len(df) == 0:
        return df
    frame = df.copy()
    for column in _NUMERIC_COLUMNS:
        if column in frame.columns:
            frame[column] = _to_float_series(frame[column])
    for column in ("volume", "status"):
        if column in frame.columns:
            frame[column] = _to_float_series(frame[column])
    frame.attrs.update(dict(df.attrs))
    return frame

def _to_float_quiet(value: Any) -> float:
    """单值转 float，失败返回 NaN。"""
    if value is None or value is pd.NA:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")

def _to_bool_quiet(value: Any) -> bool:
    """单值转 bool（None/NaN → False）。"""
    if value is None or value is pd.NA:
        return False
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating, Decimal)):
        try:
            return bool(float(value))
        except (TypeError, ValueError):
            return False
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "是"}

def _to_bool_series(series: pd.Series) -> pd.Series:
    """列转 bool（None/NaN → False），空列安全。"""
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False).astype(bool)
    return series.map(_to_bool_quiet).astype(bool)

def _normalize_dtypes(frame: pd.DataFrame) -> None:
    """统一列类型：数值列 float64、布尔列 bool、日期列 datetime。"""
    for column in _NUMERIC_COLUMNS:
        if column in frame.columns:
            frame[column] = _to_float_series(frame[column])
    for column in ("volume", "status"):
        if column in frame.columns:
            frame[column] = _to_float_series(frame[column])
    for column in _BOOL_COLUMNS:
        if column in frame.columns:
            frame[column] = _to_bool_series(frame[column])
    for column in ("list_date", "delist_date"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce").dt.normalize()
    if "code" in frame.columns:
        frame["code"] = frame["code"].astype(str).str.zfill(6)

async def _load_open_dates(db: AsyncSession, trade_date: date) -> list[date] | None:
    """读取 trade_calendar 中 <= trade_date 的开市日（用于次新股过滤）。

    表不存在时返回 None（调用方退化到自然日近似并标记 calendar_source=fallback）。
    """
    statement = (
        select(TradeCalendar.cal_date)
        .where(TradeCalendar.is_open.is_(True))
        .where(TradeCalendar.cal_date <= trade_date)
        # 多取一年：确保覆盖"上市日在 HISTORY_START 附近"的次新股计数
        .where(TradeCalendar.cal_date >= HISTORY_START - timedelta(days=400))
        .order_by(TradeCalendar.cal_date)
    )
    try:
        rows = (await db.execute(statement)).scalars().all()
    except ProgrammingError:
        logger.warning("trade_calendar 不存在，次新股过滤退化为自然日近似")
        return None
    return list(rows) if rows else None

def listed_trading_days(
    list_dates: pd.Series,
    trade_date: date,
    open_dates: Sequence[date] | None = None,
) -> pd.Series:
    """计算每只标的截至 trade_date 的上市交易日数（含上市当日之后的天数）。

    - 有交易日历时：用 searchsorted 精确计数（O(n log m)，向量化）；
    - 无交易日历时：退化为自然日近似（84 自然日 ≈ 60 交易日）并标记 NaN 由调用方处理；
    - list_date 为空的标的不做次新股剔除（数据缺失不误杀）。
    """
    normalized_dates = pd.to_datetime(list_dates, errors="coerce")
    trade_ts = pd.Timestamp(trade_date).normalize()

    if open_dates:
        calendar = pd.DatetimeIndex(sorted({pd.Timestamp(item) for item in open_dates}))
        calendar = calendar[calendar <= trade_ts]
        if len(calendar) > 0:
            values = normalized_dates.to_numpy(dtype="datetime64[ns]")
            positions = np.searchsorted(calendar.to_numpy(dtype="datetime64[ns]"), values, side="right")
            days = pd.Series(len(calendar) - positions, index=normalized_dates.index, dtype="float64")
            days[normalized_dates.isna()] = float("nan")
            # 上市日早于日历最早一日：无法精确计数，但必然"早已上市"，不做次新股剔除
            # （否则回测起点贴近历史起点时会误杀全市场）
            too_old = normalized_dates.notna() & (normalized_dates < calendar[0])
            days[too_old] = 9999.0
            return days

    # 退化：自然日近似
    delta = (trade_ts - normalized_dates).dt.days.astype("float64")
    delta[normalized_dates.isna()] = float("nan")
    return delta / 7.0 * 5.0  # 自然日 → 约当交易日

def apply_common_filters(
    df: pd.DataFrame,
    trade_date: date,
    open_dates: Sequence[date] | None = None,
) -> pd.DataFrame:
    """所有策略统一的 A 股前置过滤（纯函数）。

    剔除：ST、status != 1、delist_date 已到、上市不足 60 个交易日、
    is_suspended、当日无行情（close 为空）。
    """
    if df is None or len(df) == 0:
        empty = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        if isinstance(df, pd.DataFrame):
            empty.attrs.update(dict(df.attrs))
        return empty

    trade_ts = pd.Timestamp(trade_date).normalize()
    mask = pd.Series(True, index=df.index)

    if "is_st" in df.columns:
        mask &= ~_to_bool_series(df["is_st"])
    if "status" in df.columns:
        mask &= _to_float_series(df["status"]).fillna(0.0) == 1.0
    if "delist_date" in df.columns:
        delist = pd.to_datetime(df["delist_date"], errors="coerce")
        mask &= delist.isna() | (delist.dt.normalize() > trade_ts)
    if "is_suspended" in df.columns:
        mask &= ~_to_bool_series(df["is_suspended"])
    if "close" in df.columns:
        mask &= _to_float_series(df["close"]) > 0
    if "list_date" in df.columns:
        days = listed_trading_days(df["list_date"], trade_date, open_dates)
        mask &= days.isna() | (days >= NEW_STOCK_MIN_TRADING_DAYS)

    filtered = df.loc[mask.fillna(False)].copy()
    filtered.attrs.update(dict(df.attrs))
    return filtered

def _numeric_column(df: pd.DataFrame, column: str) -> pd.Series:
    """取数值列；缺列时返回全 NaN，保证条件计算不炸。"""
    if column not in df.columns:
        return pd.Series(float("nan"), index=df.index, dtype="float64")
    return _to_float_series(df[column])

#: 短线指标在候选池中的最低覆盖率。只判断"是否全空"会让回填到一半时
#: 用几百只股票跑出信号并写入 stg_run 缓存（特征表行数不变，data_version
#: 也不会失效），必须按覆盖率拦截。
_MIN_FEATURE_COVERAGE = 0.5

def _assert_data_ready(algo_code: str, df: pd.DataFrame) -> None:
    """策略所需数据集为空时给出明确原因（E1：不要静默返回空结果）。"""
    spec = get_spec(algo_code)
    if df is None or len(df) == 0:
        raise DataUnavailableError(
            f"{str(df.attrs.get('trade_date') or '')} 候选池为空："
            "行情/上市天数/停牌过滤后没有可用标的",
            next_action="POST /api/v1/admin/ingest/history_fast?start=2023-01-01&background=true",
            detail={"dataset": "fact_stock_daily"},
        )

    def _all_null(column: str) -> bool:
        return column not in df.columns or _to_float_series(df[column]).notna().sum() == 0

    def _coverage(column: str) -> float:
        if column not in df.columns or len(df) == 0:
            return 0.0
        return float(_to_float_series(df[column]).notna().mean())

    if spec.code in _WINDOW_STRATEGIES:
        ready_column = {
            "c1": "high20_prev",
            "c2": "high40",
            "c3": "roc120",
        }[spec.code]
        if _all_null(ready_column):
            raise DataUnavailableError(
                f"{str(df.attrs.get('trade_date') or '')} 历史窗口不足，无法执行 {spec.code.upper()} 策略",
                next_action="确认历史日线已回补到至少 121 个交易日前",
                detail={"dataset": "fact_stock_daily", "column": ready_column},
            )
        return

    if spec.code == "c6":
        for column in ("boll_width_pct60", "vol_ma5", "amount_ma20"):
            if _all_null(column):
                raise DataUnavailableError(
                    "布林/量能指标尚未生成，无法执行布林挤压策略",
                    next_action="执行指标回填（scripts/pipeline_cli.py features）后重试",
                    detail={"dataset": "fact_feature_daily", "column": column},
                )
        return

    if spec.code == "a3" and _all_null("composite_score"):
        raise DataUnavailableError(
            "个股因子评分尚未生成，无法执行综合评分策略",
            next_action="POST /api/v1/admin/ingest/scores",
            detail={"dataset": "fact_stock_score_daily"},
        )
    if spec.code == "a3":
        ownership = df["ownership"].fillna("") if "ownership" in df.columns else pd.Series(dtype=str)
        if not (ownership == "央企").any():
            raise DataUnavailableError(
                "股票池缺少央企分类，无法执行央企综合评分策略",
                next_action="POST /api/v1/admin/ingest/universe",
                detail={"dataset": "dim_stock.ownership"},
            )
    if spec.code == "a4":
        if _all_null("dv_ttm"):
            raise DataUnavailableError(
                "股息率字段为空，无法执行高股息策略",
                next_action="POST /api/v1/admin/ingest/history_fast?background=true",
                detail={"dataset": "fact_stock_daily.dv_ttm"},
            )
        if _all_null("pe_ttm"):
            raise DataUnavailableError(
                "PE 估值字段为空，无法执行高股息策略",
                next_action="POST /api/v1/admin/ingest/valuation_fast?background=true",
                detail={"dataset": "fact_stock_daily.pe_ttm"},
            )
        if _all_null("pb"):
            raise DataUnavailableError(
                "PB 估值字段为空，无法执行高股息质量策略",
                next_action="POST /api/v1/admin/ingest/valuation_fast?background=true",
                detail={"dataset": "fact_stock_daily.pb"},
            )
        if _all_null("composite_score"):
            raise DataUnavailableError(
                "个股因子评分尚未生成，无法执行高股息质量策略",
                next_action="POST /api/v1/admin/ingest/scores",
                detail={"dataset": "fact_stock_score_daily"},
            )
        for column in ("amount_ma20", "ma120"):
            coverage = _coverage(column)
            if coverage < _MIN_FEATURE_COVERAGE:
                raise DataUnavailableError(
                    f"{str(df.attrs.get('trade_date') or '')} 高股息策略所需技术指标覆盖不足"
                    f"（{column} {coverage:.0%}，需 ≥{_MIN_FEATURE_COVERAGE:.0%}），"
                    "可能仍在回填",
                    next_action="执行指标回填（scripts/pipeline_cli.py features）后重试",
                    detail={
                        "dataset": "fact_feature_daily",
                        "column": column,
                        "coverage": round(coverage, 4),
                    },
                )

__all__ = [
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
]
