"""
历史估值源：PE_TTM / PB / 股息率_TTM（A4 高股息、A3 价值质量因子的数据来源）
====================================================================================

问题
----
真实日线（baostock / 东财 ``stock_zh_a_hist``）**不含估值字段**，所以纯真实数据上
A4（高股息）、A3（价值/质量）两个策略出不了信号。本模块用
「新浪财报指标 + 已有的日线收盘价」把这三种因子补出来，并写回
``fact_stock_daily.pe_ttm / pb / dv_ttm``。

三阶段设计（**重要**）
----------------------
PE = 收盘价 / EPS_TTM，PS = 总市值 / 营业总收入_TTM，股息率 = 近 12 个月每股分红 / 收盘价：
**分子只随财报/分红公告变（季度/年度），价格每天变**。如果每天为全市场 5000+ 只股票
各打一次接口（合规限频 ≤2 请求/秒），光财报就要 45 分钟，15:36 的盘后流水线根本
跑不完。所以拆成四段：

    阶段 1a sync_financials()   抓财报指标 → ``dim_stock_financial``（慢，有网络，季度跑）
    阶段 1b sync_dividends()    抓现金分红 → ``dim_stock_dividend``（慢，有网络，月度跑）
    阶段 1c sync_revenue_bulk() 东财业绩报表 → ``dim_stock_financial.revenue``
            （按报告期批量，一个报告期一次全市场；新浪指标表没有绝对营收）
    阶段 2  compute_valuation() 库里的财报/分红 + 库里的收盘价/总市值 → 算 PE/PB/PS/股息率
            写回日线 —— **零网络**，全市场单日秒级，可每日/按区间反复跑

:func:`backfill_valuation` / :func:`collect_latest_valuation` 是给 ``app/pipeline.py``
用的便捷入口，内部就是「阶段 1a + 1b + 1c + 2」。

口径（写死在这里，全系统唯一）
------------------------------
- ``eps`` / ``bps`` 是新浪财报里的**每股**口径（元），累计值；
- ``eps_ttm``: 报告期 MMDD=1231 时 = 该期值；否则 = 本期累计 + 上年年报 − 上年同期
  （详见 :func:`eps_ttm_series` 的注释）；
- ``pe_ttm = close / eps_ttm``：**允许负值**（亏损股的 PE 为负，由策略层过滤 PE<=0）；
- ``pb = close / bps``：``bps <= 0``（净资产为负的资不抵债股）→ NULL；
- ``ps_ttm = market_cap / revenue_ttm``：营收 TTM 与 EPS TTM 同一套三期公式；总市值或
  营收缺失 → NULL，写库时 ``COALESCE`` 保留已有值（快照/东财回补的 PS 不会被洗掉）；
- ``dv_ttm(%)``：近 12 个月（``ex_date ∈ (d-365, d]``）每股现金分红合计 / 收盘价 × 100，
  数据源是**巨潮分红明细**（``stock_dividend_cninfo``），按**除权日**做 point-in-time。
  ``payout_ratio``（新浪"股息发放率"）不再用于股息率 —— 它大量为空，且语义是
  "分红/净利润"（分红占利润比例）而不是"分红/股价"，两者不是一回事；该列仍入库作参考。
  完全没有分红记录 → 0.0（真实的"无股息"，不是数据缺失）；没有价格 → NULL。

EPS / BPS 的取值优先级（踩过坑，务必别改回去）
---------------------------------------------
- ``eps`` 优先 ``每股收益_调整后``，缺失再退 ``摊薄每股收益``（实测两者差异小，调整后更可比）；
- ``bps`` **优先 ``每股净资产_调整前``，缺失才退 ``每股净资产_调整后``**。
  原因：``每股净资产_调整后`` 存在脏数据 —— 实测 688981（中芯国际）
  ``调整后=594.8588`` 而 ``调整前=25.7375``（真实 BPS 约 19–26）。用"调整后"算 PB
  会错 20 倍，A1 的价值筛选会直接崩掉。EPS 的"调整后"没有这个问题。

为什么不直接抓现成的 PE/PB/股息率
---------------------------------
联网实测（2026-10）三个常见接口都不可用：``stock_a_indicator_lg``（akshare 1.19.1 已移除）、
``stock_zh_valuation_baidu``（网络不可达）、``stock_value_em``（东财被墙）、
``stock_individual_spot_xq``（雪球要 token）。新浪财报指标接口
``stock_financial_analysis_indicator`` 可用，所以只能「自己算」。

未来函数（point-in-time）防线
-----------------------------
财报与分红都有**披露/实施滞后**，不能用"未来才公开的信息"算"过去的估值"：

- 阶段 1a 落库时写入 ``announced_date = report_date + 报告类型对应滞后``，阶段 2 只用
  ``announced_date <= 交易日`` 的最新报告期（``build_valuation_frame`` 的
  ``merge_asof(direction="backward")`` 做的就是这个）；
- 分红用**除权日**（``ex_date``）作为市场可知日：``d >= ex_date`` 才计入当天的近 12 个月
  分红（见 :func:`dv_ttm_series`）。

滞后按法定披露期限取值（自然日）：一季报/三季报 30 天（4/30、10/31 前）、半年报
62 天（8/31 前）、年报 120 天（次年 4/30 前），见 :data:`REPORT_LAG_DAYS_BY_MONTH`。
统一取 90 天会让三季报晚可用两个月（10/30 → 12/29）：快照抓取失败时，零网络兜底
只能用半年报甚至更早的数据算估值，PS 会因营收 TTM 为负/缺失而留空；按报告类型取
法定上限既不会提前使用，也与市场实际可见时间一致。

注意：新浪财报接口 ``start_year=2019`` 时实测**最多只返回最近 14–15 期**（约 3.5 年），
拿不到更早的年度就如实留空，**绝不做线性外推**（外推出来的 PE 没有可信度）。

其他工程约定
------------
- **不在 import 期联网**：``akshare`` 用 try/except 导入，缺依赖时 ``ak=None`` 全部降级为
  「返回空表 + 记日志」；
- 所有 async DB 函数都接受 ``db: AsyncSession | None``：``None`` 时用
  ``app.db.session.session_scope()``（自己 commit），传入时**只 flush 不 commit**
  （事务所有权归 ``pipeline``）；
- 限频统一走 :func:`app.ingest.base.make_history_limiter`（≤2 请求/秒），在**每个**网络
  请求前 acquire，不在调用方重复节流；
- 写库只 ``UPDATE`` 那三列，**绝不 INSERT / 不覆盖 close、volume、amount 等行情列**
  （用 ``UPDATE ... FROM (VALUES ...)`` 一条语句批量更新，实测比逐行 UPDATE 快一个量级）。
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np
import pandas as pd

try:  # pragma: no cover —— 依赖是否安装由运行环境决定
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]

from app.core import config
from app.core.logging import get_logger
from app.core.status import QualityStatus
from app.ingest.base import FetchResult, chunked, make_history_limiter
from app.ingest.quality import QualityReport, make_error_report

if TYPE_CHECKING:  # pragma: no cover —— 只为类型标注，运行期不 import 数据库栈
    from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.valuation_math import *  # noqa: F401,F403
from app.ingest.valuation_store import *  # noqa: F401,F403

logger = get_logger(__name__)

_LIMITER = make_history_limiter()

def _network_enabled() -> bool:
    """是否允许联网（离线开发/CI 用 ``INGEST_NETWORK_ENABLED=0`` 全链路降级）。"""
    return bool(getattr(config, "INGEST_NETWORK_ENABLED", True))

async def _ak_call(func_name: str, **kwargs: Any) -> pd.DataFrame | None:
    """调用一个 akshare 函数：线程池执行 + 限频 + 异常收敛（任何失败都返回 None，不抛）。

    这是本模块**唯一**的联网出口。阶段 2（:func:`compute_valuation`）完全不经过这里，
    所以它的"零网络"性质可以直接用"把这个函数换成会抛异常的桩"来测（见测试）。
    """
    if ak is None:
        logger.warning("akshare 未安装，跳过财报抓取（%s）", func_name)
        return None
    if not _network_enabled():
        logger.info("INGEST_NETWORK_ENABLED=0，跳过联网抓取（%s）", func_name)
        return None
    func = getattr(ak, func_name, None)
    if func is None:
        # 版本差异：接口被移除/改名（例如 1.19.1 已经没有 stock_a_indicator_lg）
        logger.warning("akshare 缺少函数 %s（版本差异），跳过", func_name)
        return None
    await _LIMITER.acquire()
    try:
        result = await asyncio.to_thread(func, **kwargs)
    except NotImplementedError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 外部库异常类型不可控，统一收敛成"无数据"
        logger.warning("akshare 调用失败 %s: %s: %s", func_name, type(exc).__name__, exc)
        return None
    if not isinstance(result, pd.DataFrame) or result.empty:
        return None
    return result

def _normalize_code(code: Any) -> str:
    """统一 6 位股票代码（去后缀/补零）。"""
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else str(code)

def _sina_symbol(code: str) -> str:
    """6 位代码 → 新浪接口的带交易所前缀代码（sh600519 / sz000001 / bj830799 / bj920000）。

    920xxx 是北交所，必须先于"9 开头 = 沪市"判断。
    """
    digits = _normalize_code(code)
    if digits.startswith(("4", "8", "920")):
        return f"bj{digits}"
    if digits.startswith(("6", "9")):
        return f"sh{digits}"
    if digits.startswith(("0", "2", "3")):
        return f"sz{digits}"
    return f"sh{digits}"

def _extract_revenue_map(raw: pd.DataFrame | None) -> dict[date, float]:
    """从财报指标原始表里抽 ``report_date -> 营业总收入``（列不存在就返回空 dict）。"""
    if raw is None or len(raw) == 0:
        return {}
    date_column = _resolve_column(raw.columns, FIN_DATE_CANDIDATES, FIN_DATE_PATTERNS)
    revenue_column = _resolve_column(raw.columns, FIN_REVENUE_CANDIDATES, FIN_REVENUE_PATTERNS)
    if date_column is None or revenue_column is None:
        return {}
    periods = _date_series(raw[date_column])
    values = _numeric_column(raw, revenue_column)
    if values is None:
        return {}
    mapping: dict[date, float] = {}
    for period, value in zip(periods, values):
        if pd.isna(period) or pd.isna(value):
            continue
        mapping[period] = float(value)
    return mapping

def normalize_abstract_metrics(raw: pd.DataFrame | None) -> pd.DataFrame:
    """把新浪 ``stock_financial_abstract``（``指标`` 行 × ``报告期`` 列）转成宽表。

    输出列：``[report_date, net_profit, revenue, roe, total_share]``（按报告期升序）。

    为什么需要它：``stock_financial_analysis_indicator`` 偶尔缺 EPS 列（版本差异），
    财务摘要里的「归母净利润」是最后一道兜底：EPS = 归母净利润 / 股本（近似，见
    :func:`abstract_eps_frame` 的注释）。
    """
    columns = ["report_date", "net_profit", "revenue", "roe", "total_share"]
    if raw is None or len(raw) == 0:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})

    metric_column = _resolve_column(
        raw.columns, ABSTRACT_METRIC_CANDIDATES, ABSTRACT_METRIC_PATTERNS
    )
    if metric_column is None:
        logger.warning("财务摘要缺少「指标」列，无法解析")
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})

    # 除「指标」列外，列名就是报告期（"20260630"）
    periods: list[tuple[Any, date]] = []
    for column in raw.columns:
        if column == metric_column:
            continue
        parsed = _parse_period(column)
        if parsed is not None:
            periods.append((column, parsed))
    if not periods:
        logger.warning("财务摘要未识别出任何报告期列（列名样例：%s）",
                       [str(c) for c in list(raw.columns)[:8]])
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})

    targets = {
        "net_profit": (ABSTRACT_NET_PROFIT, ABSTRACT_NET_PROFIT_PATTERNS),
        "revenue": (ABSTRACT_REVENUE, ABSTRACT_REVENUE_PATTERNS),
        "roe": (ABSTRACT_ROE, ABSTRACT_ROE_PATTERNS),
        "total_share": (ABSTRACT_TOTAL_SHARE, ABSTRACT_TOTAL_SHARE_PATTERNS),
    }
    row_of: dict[str, Any] = {}
    for _, row in raw.iterrows():
        name = str(row[metric_column]).strip()
        for target, (candidates, patterns) in targets.items():
            if target in row_of:
                continue
            if name in candidates or any(
                re.search(pattern, name) for pattern in patterns
            ):
                row_of[target] = row
                break

    if "net_profit" not in row_of:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})

    records: list[dict[str, Any]] = []
    for column, period in periods:
        item: dict[str, Any] = {"report_date": period}
        for target in targets:
            row = row_of.get(target)
            item[target] = _num(row[column]) if row is not None else None
        records.append(item)
    frame = pd.DataFrame(records, columns=columns)
    frame = frame.sort_values("report_date", kind="mergesort").reset_index(drop=True)
    return frame

def abstract_eps_frame(
    metrics: pd.DataFrame,
    fallback_share: float | None = None,
) -> pd.DataFrame:
    """由财务摘要的「归母净利润 / 股本」反推 EPS（**近似兜底**，仅在指标表缺 EPS 时用）。

    口径说明（务必了解其局限）：
        - 有「总股本」行 → 用当期股本，口径接近真实摊薄 EPS；
        - 没有股本 → 用 ``fallback_share``（新浪日线的 ``outstanding_share``，**流通股本**），
          流通股本 < 总股本时 EPS 会被高估、PE 被低估。这属于"有数总比没有强"的兜底，
          日志会明确标记，便于事后核对。

    返回 ``[report_date, eps, roe, revenue]``；股本完全拿不到时返回空表（宁缺勿错）。
    """
    columns = ["report_date", "eps", "roe", "revenue"]
    if metrics is None or len(metrics) == 0:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    frame = metrics.copy()
    if "net_profit" not in frame.columns or "report_date" not in frame.columns:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})

    net_profit = pd.to_numeric(frame["net_profit"], errors="coerce")
    share = (
        pd.to_numeric(frame["total_share"], errors="coerce")
        if "total_share" in frame.columns
        else pd.Series(np.nan, index=frame.index, dtype="float64")
    )
    if fallback_share is not None and fallback_share > 0:
        share = share.where(share.notna() & (share > 0), float(fallback_share))
    eps = net_profit / share
    eps = _finite(eps.where(net_profit.notna() & share.notna() & (share > 0)))

    result = pd.DataFrame(
        {
            "report_date": _date_series(frame["report_date"]),
            "eps": eps,
            "roe": pd.to_numeric(frame["roe"], errors="coerce")
            if "roe" in frame.columns
            else np.nan,
            "revenue": pd.to_numeric(frame["revenue"], errors="coerce")
            if "revenue" in frame.columns
            else np.nan,
        }
    )
    result = result.dropna(subset=["report_date"])
    if result.empty:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})
    return result.sort_values("report_date", kind="mergesort").reset_index(drop=True)

async def _fetch_total_share(code: str) -> float | None:
    """兜底股本：新浪日线 ``stock_zh_a_daily`` 的 ``outstanding_share``（流通股本，股）。

    只在指标表缺 EPS、财务摘要又缺「总股本」时才会被调用（多一次请求，日记级别日志）。
    """
    raw = await _ak_call("stock_zh_a_daily", symbol=_sina_symbol(code))
    if raw is None:
        return None
    column = _resolve_column(
        raw.columns, ("outstanding_share", "流通股本"), (r"^outstanding_share$", r"流通股本")
    )
    if column is None:
        return None
    values = pd.to_numeric(raw[column], errors="coerce").dropna()
    if len(values) == 0:
        return None
    return float(values.iloc[-1])

def _merge_abstract_eps(fins: pd.DataFrame, abstract: pd.DataFrame) -> pd.DataFrame:
    """把兜底 EPS/ROE 合并进主表：**只填补缺失，不覆盖**新浪指标表已有值。"""
    if abstract is None or len(abstract) == 0:
        return fins
    if fins is None or len(fins) == 0:
        frame = abstract.copy()
        for column in ("bps", "payout_ratio"):
            frame[column] = np.nan
        return frame[list(FIN_COLUMNS)].sort_values("report_date").reset_index(drop=True)

    frame = fins.merge(
        abstract.rename(columns={"eps": "eps_abstract", "roe": "roe_abstract"}),
        on="report_date",
        how="outer",
    )
    frame["eps"] = frame["eps"].where(frame["eps"].notna(), frame["eps_abstract"])
    frame["roe"] = frame["roe"].where(frame["roe"].notna(), frame["roe_abstract"])
    frame = frame.drop(columns=["eps_abstract", "roe_abstract"])
    for column in ("bps", "payout_ratio"):
        if column not in frame.columns:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame[list(FIN_COLUMNS)].sort_values("report_date").reset_index(drop=True)

async def fetch_financials(code: str, start_year: int = DEFAULT_START_YEAR) -> pd.DataFrame:
    """联网抓单只股票的财报指标（新浪），返回归一化 + TTM 后的表。

    数据源与降级：
        1. 主源 ``ak.stock_financial_analysis_indicator(symbol, start_year)`` ——
           每个报告期一行，含 EPS/BPS/ROE/股息发放率；
        2. 兜底 ``ak.stock_financial_abstract(symbol)`` —— **仅在主源有数据但缺 EPS 时**，
           用「归母净利润 / 股本」反推 EPS（见 :func:`abstract_eps_frame` 的局限性说明）；
           主源整体为空（网络/接口失败）时直接返回空表，不做兜底覆盖。
        3. 股本兜底 ``ak.stock_zh_a_daily`` 的 ``outstanding_share``（流通股本近似）。

    明确**不用**的接口（2026-10 实测不可用）：``stock_a_indicator_lg``（akshare 1.19.1
    已移除）、``stock_zh_valuation_baidu``（网络不可达）、``stock_value_em``（东财被墙）、
    ``stock_individual_spot_xq``（雪球要 token）。

    Args:
        code: 股票代码（600519 / sh600519 都可以）。
        start_year: 起始年份（默认 2019，覆盖近 5 年回补 + TTM 需要的上年数据）。

    Returns:
        ``[report_date, eps, bps, roe, payout_ratio, eps_ttm, eps_ttm_basis, revenue]``，
        按报告期升序。**任何失败都返回空表并记日志**，不抛异常（单只失败不影响批量任务）。
    """
    normalized = _normalize_code(code)
    raw = await _ak_call(
        "stock_financial_analysis_indicator",
        symbol=normalized,
        start_year=str(int(start_year)),
    )
    fins = normalize_financials(raw)
    revenue_map = _extract_revenue_map(raw)

    if fins.empty:
        # 主源整体不可用（DNS/限频/接口失败）时**不能**走摘要兜底：摘要是
        # 「归母净利润 / 股本」的近似口径，且没有 BPS。实测 688836（宇树科技-W）
        # 一次 DNS 抖动就把库里拆股前 495.08 的 BPS 洗成 NULL、PB 全空，
        # 同时用不同股本基期重算了 EPS。失败就返回空表，由 sync_financials
        # 记为 failed 并留待下次重试，绝不用降级源覆盖已入库数据。
        logger.warning("财报指标表主源为空（网络/接口失败），跳过摘要兜底 code=%s", normalized)
        return _empty_financials_ttm()

    if fins["eps"].isna().all():
        logger.info("财报指标表缺 EPS，尝试财务摘要兜底 code=%s", normalized)
        metrics_raw = await _ak_call("stock_financial_abstract", symbol=normalized)
        metrics = normalize_abstract_metrics(metrics_raw)
        if not metrics.empty:
            has_period_share = False
            if "total_share" in metrics.columns:
                shares = pd.to_numeric(metrics["total_share"], errors="coerce")
                has_period_share = bool(shares.notna().any())
            # 摘要里已有当期股本就不用再打日线接口；否则用流通股本近似（多一次请求）
            fallback_share: float | None = (
                None if has_period_share else await _fetch_total_share(normalized)
            )
            abstract_frame = abstract_eps_frame(metrics, fallback_share)
            fins = _merge_abstract_eps(fins, abstract_frame)
            for period, value in zip(metrics["report_date"], metrics["revenue"]):
                if pd.notna(period) and pd.notna(value):
                    revenue_map.setdefault(period, float(value))

    if fins.empty:
        return _empty_financials_ttm()

    ttm = eps_ttm_series(fins)
    result = fins.merge(
        ttm[["report_date", "eps_ttm", "eps_ttm_basis"]], on="report_date", how="left"
    )
    result["revenue"] = result["report_date"].map(revenue_map)
    return result[list(FIN_COLUMNS) + ["eps_ttm", "eps_ttm_basis", "revenue"]]

async def sync_financials(
    codes: list[str] | None = None,
    start_year: int = DEFAULT_START_YEAR,
    db: "AsyncSession | None" = None,
    only_missing: bool = True,
    resume: bool = True,
) -> FetchResult:
    """阶段 1：抓财报指标 → 落库 ``dim_stock_financial``（慢，带网络，断点续传）。

    为什么单独一个阶段：财报是季度数据，每天为全市场重抓一次要 45 分钟（合规限频
    ≤2 请求/秒）。落库缓存后，每日任务只需 ``compute_valuation``（零网络、秒级）。

    Args:
        codes: 指定股票代码；``None`` = ``dim_stock`` 里 ``status=1``（在市）的全部标的。
        start_year: 起始年份（默认 2019；``backfill_valuation`` 会按回补起点自动往前推）。
        db: 外部 ``AsyncSession``（传入只 flush 不 commit，事务归 pipeline）；
            ``None`` 时用 ``session_scope()`` 自建事务。
        only_missing: ``True``（默认）时跳过"已有最新可得报告期"的股票 —— 每日增量几乎
            零请求。注意：**显式传 codes 时同样生效**，要强制刷新请传 ``only_missing=False``。
        resume: 是否读 ``ingest_job.checkpoint`` 跳过已完成代码（长任务中断后重跑用）。

    Returns:
        ``FetchResult``：``rows`` = upsert 的财报表行数，``total`` = 处理的股票数，
        ``failed`` = 抓取失败的股票数。``detail`` 里带 ``pending``/``skipped_fresh``/
        ``failed_codes`` 等排查信息。

    口径说明：``announced_date = report_date + 报告类型对应的法定披露滞后``（point-in-time
    的时间锚），由 :func:`_financial_rows_from_frame` 统一写入。
    """
    started = time.perf_counter()
    task = "sync_financials"
    today = _today_cn()

    code_list = _normalize_codes(codes) if codes is not None else await _resolve_market_codes(db)
    if not code_list:
        logger.warning("财报同步：股票池为空（codes=%s），跳过", codes)
        return FetchResult(
            rows=0, total=0, elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"skipped": "no_codes", "task": task},
        )

    scope = (
        "all" if codes is None
        else "codes:" + ",".join(code_list[:5]) + f"..({len(code_list)})"
    )
    params = {"start_year": int(start_year), "scope": scope}

    done: set[str] = set()
    failed: dict[str, str] = {}
    written = 0
    skipped_fresh = 0

    # only_missing：已有"最新可得报告期"的股票不用再联网
    pending = list(code_list)
    if only_missing:
        required = latest_knowable_report_period(today)
        try:
            latest = await _load_latest_financial_dates(db, code_list)
        except Exception:  # noqa: BLE001 —— 读不到的代码按"需要抓取"处理更安全
            logger.exception("读取已入库报告期失败，本次全量抓取")
            latest = {}
        pending = []
        for code in code_list:
            period = latest.get(code)
            if required is not None and period is not None and period >= required:
                done.add(code)
                skipped_fresh += 1
            else:
                pending.append(code)
    logger.info(
        "财报同步：共 %d 只，待抓 %d 只，跳过（已有最新报告）%d 只",
        len(code_list), len(pending), skipped_fresh,
    )

    if resume:
        checkpoint = await _load_checkpoint(db, task, DATASET, params)
        for code in checkpoint.get("completed_codes", []) or []:
            done.add(str(code))
        for code, reason in (checkpoint.get("failed_codes") or {}).items():
            failed[str(code)] = str(reason)
        if checkpoint:
            pending = [code for code in pending if code not in done]
            logger.info("财报同步断点续传：跳过已完成 %d 只", len(done))

    job_id = await _job_begin(db, task, DATASET, params, total=len(code_list))
    batch_rows: list[dict[str, Any]] = []
    batch_codes: list[str] = []

    async def _flush_batch() -> None:
        nonlocal written
        if not batch_rows:
            batch_codes.clear()
            return
        written += await _upsert_financial_rows(db, batch_rows)
        done.update(batch_codes)
        batch_rows.clear()
        batch_codes.clear()

    try:
        for index, code in enumerate(pending, start=1):
            try:
                frame = await fetch_financials(code, start_year=start_year)
                if frame.empty:
                    failed[code] = "financials_empty"
                else:
                    rows = _financial_rows_from_frame(code, frame)
                    if not rows:
                        failed[code] = "no_valid_report_period"
                    else:
                        batch_rows.extend(rows)
                        batch_codes.append(code)
            except Exception as exc:  # noqa: BLE001 —— 单只失败不影响整体
                failed[code] = f"{type(exc).__name__}: {exc}"
                logger.warning("财报抓取失败 code=%s：%s", code, exc)

            if len(batch_codes) >= SYNC_BATCH_CODES:
                await _flush_batch()
            if index % SYNC_CHECKPOINT_EVERY == 0 or index == len(pending):
                await _flush_batch()
                logger.info(
                    "财报同步进度 %d/%d（完成 %d，失败 %d，写入 %d 行）",
                    index, len(pending), len(done), len(failed), written,
                )
                await _job_update(
                    db,
                    job_id,
                    done=len(done),
                    failed=len(failed),
                    rows_written=written,
                    checkpoint={
                        "params": params,
                        "completed_codes": sorted(done),
                        "failed_codes": dict(sorted(failed.items())[:500]),
                    },
                )

        await _flush_batch()
        elapsed = int((time.perf_counter() - started) * 1000)

        coverage = len(done) / len(code_list) if code_list else 0.0
        report = QualityReport(
            dataset=DATASET,
            rows=written,
            expected=len(code_list),
            coverage=round(coverage, 4),
        )
        if failed:
            report.add(
                "financials_fetch_failed",
                count=len(failed),
                severity="warning",
                message=f"{len(failed)} 只标的财报抓取失败（未写库，重跑可补）",
                samples=sorted(failed)[:5],
            )
        status = (
            QualityStatus.OK if written > 0 or not pending else QualityStatus.ERROR
        )
        if status is QualityStatus.ERROR:
            await _record_quality(
                db,
                make_error_report(DATASET, "全部标的财报抓取失败"),
                today,
                QualityStatus.ERROR,
            )
        else:
            await _record_quality(db, report, today, status)

        final_checkpoint = {
            "params": params,
            "completed_codes": sorted(done),
            "failed_codes": dict(sorted(failed.items())[:500]),
        }
        await _job_finish(
            db,
            job_id,
            "success" if not failed else "partial",
            started_at=started,
            done=len(done),
            failed=len(failed),
            rows_written=written,
            checkpoint=final_checkpoint,
        )
        return FetchResult(
            rows=written,
            total=len(code_list),
            failed=len(failed),
            elapsed_ms=elapsed,
            detail={
                "task": task,
                "completed": len(done),
                "skipped_fresh": skipped_fresh,
                "pending": len(pending),
                "failed_codes": sorted(failed)[:100],
                "start_year": int(start_year),
            },
        )
    except Exception as exc:
        await _job_finish(
            db,
            job_id,
            "failed",
            started_at=started,
            done=len(done),
            failed=len(failed),
            rows_written=written,
            checkpoint={
                "params": params,
                "completed_codes": sorted(done),
                "failed_codes": dict(sorted(failed.items())[:500]),
            },
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

async def _sync_dividends_bulk(
    db: "AsyncSession | None", code_list: Sequence[str], today: date
) -> int:
    """东财全市场分红实施记录一次性回补（巨潮不可用时的主路径）。

    ``RPT_SHAREBONUS_DET`` 的 ``PRETAX_BONUS_RMB`` 是每 10 股税前金额，入库换算成
    每股 ``dps``。只更新 ``dps/src``，保留巨潮已写入的 ``announce_date``。
    失败抛 :class:`ProviderError`，由 :func:`sync_dividends` 降级逐只巨潮。
    """
    from app.ingest.em_dividends import fetch_dividends

    start = date(today.year - DIVIDEND_BULK_YEARS, today.month, today.day)
    events = await fetch_dividends(start, today)
    allowed = set(_normalize_codes(code_list))
    rows: list[dict[str, Any]] = []
    written = 0
    for code, items in events.items():
        if code not in allowed:
            continue
        for ex_date, per_10 in items:
            dps = _as_db_value(float(per_10) / 10.0, 6)
            if dps is None or dps <= 0:
                continue
            rows.append({
                "code": code, "ex_date": ex_date, "dps": dps,
                "announce_date": None, "report_period": None, "src": "eastmoney",
            })
            if len(rows) >= DIVIDEND_BULK_CHUNK:
                written += await _upsert_dividend_rows(
                    db, rows, update_columns=["dps", "src"]
                )
                rows = []
    written += await _upsert_dividend_rows(db, rows, update_columns=["dps", "src"])
    return written

async def sync_dividends(
    codes: list[str] | None = None,
    db: "AsyncSession | None" = None,
    only_missing: bool = True,
    resume: bool = True,
) -> FetchResult:
    """阶段 1b：抓现金分红明细 → 落库 ``dim_stock_dividend``（慢，带网络，月度跑）。

    为什么要这一层：新浪财报的 ``股息发放率(%)`` 大量为空，而且它的语义是
    "分红 / 净利润"（分红占利润的比例），**不是**股息率。股息率 =
    近 12 个月每股现金分红 / 股价，必须用分红明细（巨潮 ``stock_dividend_cninfo``，
    实测 600519 返回 31 行）按**除权日**落库，才能做 point-in-time。

    Args:
        codes: 指定股票代码；``None`` = ``dim_stock`` 里 ``status=1`` 的全部标的。
        db: 外部 ``AsyncSession``（只 flush）；``None`` 时 ``session_scope()`` 自建事务。
        only_missing: ``True``（默认）时跳过近期已刷新过的股票：
            - 有分红记录的股票看 ``dim_stock_dividend.updated_at``
              （``DIVIDEND_REFRESH_DAYS`` = 30 天）；
            - **没有分红记录**的股票靠上一次任务 checkpoint 里的 ``checked_codes``
              + ``checked_at`` 兜住（否则"从没分红"的股票会被每天重抓）。
        resume: 是否复用上次 checkpoint 跳过已完成代码。

    Returns:
        ``FetchResult``：``rows`` = upsert 的分红行数，``total`` = 处理的股票数，
        ``failed`` = 抓取失败的股票数。

    说明：``ex_date`` 用「除权日」；没有除权日时退「派息日」/「公告日」并记 warning
    （见 :func:`normalize_dividends`）。完全没分红的股票 = 查询成功但没有行，
    仍算"已检查"，写进 checkpoint 的 ``checked_codes``，下次直接跳过。
    """
    started = time.perf_counter()
    task = "sync_dividends"
    today = _today_cn()

    code_list = _normalize_codes(codes) if codes is not None else await _resolve_market_codes(db)
    if not code_list:
        logger.warning("分红同步：股票池为空（codes=%s），跳过", codes)
        return FetchResult(
            rows=0, total=0, elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"skipped": "no_codes", "task": task},
        )

    scope = (
        "all" if codes is None
        else "codes:" + ",".join(code_list[:5]) + f"..({len(code_list)})"
    )
    params = {"scope": scope}

    done: set[str] = set()
    failed: dict[str, str] = {}
    written = 0
    skipped_fresh = 0
    pending = list(code_list)

    if only_missing:
        # 1) 有分红记录、且最近 DIVIDEND_REFRESH_DAYS 内刷新过的 → 跳过
        recent: set[str] = set()
        try:
            updated = await _load_dividend_updated(db, code_list)
        except Exception:  # noqa: BLE001 —— 读不到就全量重抓（安全侧）
            logger.exception("读取分红刷新时间失败，本次全量抓取")
            updated = {}
        now = datetime.now(timezone.utc)
        for code, updated_at in updated.items():
            last = updated_at
            if last.tzinfo is None:  # 防御：DB 返回 naive datetime
                last = last.replace(tzinfo=timezone.utc)
            if (now - last) <= timedelta(days=DIVIDEND_REFRESH_DAYS):
                recent.add(code)

        # 2) "查过但没有分红"的股票：靠上次 checkpoint 的 checked_at/checked_codes 跳过
        try:
            checkpoint = await _load_checkpoint(db, task, DATASET, params)
        except Exception:  # noqa: BLE001
            logger.exception("读取分红断点失败")
            checkpoint = {}
        if checkpoint:
            checked_at = checkpoint.get("checked_at")
            if checked_at:
                try:
                    checked_date = _as_date(checked_at)
                except ValueError:
                    checked_date = None
                if (
                    checked_date is not None
                    and (today - checked_date).days < DIVIDEND_REFRESH_DAYS
                ):
                    recent.update(
                        str(code) for code in checkpoint.get("checked_codes") or []
                    )
            for code in checkpoint.get("completed_codes") or []:
                done.add(str(code))
        skipped_fresh = len([code for code in code_list if code in recent])
        done.update(recent)
        pending = [code for code in code_list if code not in recent]

    if resume:
        checkpoint = await _load_checkpoint(db, task, DATASET, params)
        for code in checkpoint.get("completed_codes") or []:
            done.add(str(code))
        for code, reason in (checkpoint.get("failed_codes") or {}).items():
            failed[str(code)] = str(reason)
        if checkpoint:
            pending = [code for code in pending if code not in done]
            logger.info("分红同步断点续传：跳过已完成 %d 只", len(done))

    logger.info(
        "分红同步：共 %d 只，待抓 %d 只，跳过（近期已刷新）%d 只",
        len(code_list), len(pending), skipped_fresh,
    )

    job_id = await _job_begin(db, task, DATASET, params, total=len(code_list))
    batch_rows: list[dict[str, Any]] = []
    batch_codes: list[str] = []
    source_tag = "cninfo"

    async def _flush_batch() -> None:
        nonlocal written
        if not batch_rows:
            batch_codes.clear()
            return
        written += await _upsert_dividend_rows(db, batch_rows)
        done.update(batch_codes)
        batch_rows.clear()
        batch_codes.clear()

    try:
        # 巨潮按代码逐只抓在 DNS/风控不可用时整批失败；东财 RPT_SHAREBONUS_DET
        # 一次分页拉全市场，作为主路径；失败再降级逐只巨潮。
        if pending:
            try:
                written = await _sync_dividends_bulk(db, code_list, today)
                done.update(pending)
                failed.clear()  # 旧断点里的逐只失败已由批量全量覆盖
                source_tag = "eastmoney"
                logger.info(
                    "分红同步：东财批量完成，%d 只，%d 行", len(pending), written
                )
            except Exception as exc:  # noqa: BLE001 —— 降级逐只巨潮
                logger.warning("东财批量分红失败，降级逐只巨潮：%s", exc)

        if source_tag == "cninfo":
            for index, code in enumerate(pending, start=1):
                try:
                    raw = await _ak_call("stock_dividend_cninfo", symbol=code)
                    if raw is None:
                        # 查询成功但没有数据 → "无分红"，查过即完成（不写库）
                        done.add(code)
                    else:
                        rows = _dividend_rows_from_frame(code, normalize_dividends(raw))
                        if rows:
                            batch_rows.extend(rows)
                            batch_codes.append(code)  # 落库成功后才标记完成（见 _flush_batch）
                        else:
                            done.add(code)
                except Exception as exc:  # noqa: BLE001 —— 单只失败不影响整体
                    failed[code] = f"{type(exc).__name__}: {exc}"
                    logger.warning("分红抓取失败 code=%s：%s", code, exc)

                if len(batch_codes) >= SYNC_BATCH_CODES:
                    await _flush_batch()
                if index % SYNC_CHECKPOINT_EVERY == 0 or index == len(pending):
                    await _flush_batch()
                    logger.info(
                        "分红同步进度 %d/%d（检查 %d，失败 %d，写入 %d 行）",
                        index, len(pending), len(done), len(failed), written,
                    )
                    await _job_update(
                        db,
                        job_id,
                        done=len(done),
                        failed=len(failed),
                        rows_written=written,
                        checkpoint={
                            "params": params,
                            "completed_codes": sorted(done),
                            "checked_codes": sorted(done),
                            "checked_at": today.isoformat(),
                            "failed_codes": dict(sorted(failed.items())[:500]),
                        },
                    )

        await _flush_batch()
        elapsed = int((time.perf_counter() - started) * 1000)

        coverage = len(done) / len(code_list) if code_list else 0.0
        report = QualityReport(
            dataset=DATASET,
            rows=written,
            expected=len(code_list),
            coverage=round(coverage, 4),
        )
        if failed:
            report.add(
                "dividends_fetch_failed",
                count=len(failed),
                severity="warning",
                message=f"{len(failed)} 只标的分红抓取失败（未写库，重跑可补）",
                samples=sorted(failed)[:5],
            )
        status = (
            QualityStatus.OK if not pending or any(code in done for code in pending)
            else QualityStatus.ERROR
        )
        if status is QualityStatus.ERROR:
            await _record_quality(
                db, make_error_report(DATASET, "全部标的分红抓取失败"),
                today, QualityStatus.ERROR,
            )
        else:
            await _record_quality(db, report, today, status)

        final_checkpoint = {
            "params": params,
            "completed_codes": sorted(done),
            "checked_codes": sorted(done),
            "checked_at": today.isoformat(),
            "failed_codes": dict(sorted(failed.items())[:500]),
        }
        await _job_finish(
            db,
            job_id,
            "success" if not failed else "partial",
            started_at=started,
            done=len(done),
            failed=len(failed),
            rows_written=written,
            checkpoint=final_checkpoint,
        )
        return FetchResult(
            rows=written,
            total=len(code_list),
            failed=len(failed),
            elapsed_ms=elapsed,
            detail={
                "task": task,
                "source": source_tag,
                "completed": len(done),
                "skipped_fresh": skipped_fresh,
                "pending": len(pending),
                "failed_codes": sorted(failed)[:100],
            },
        )
    except Exception as exc:
        await _job_finish(
            db,
            job_id,
            "failed",
            started_at=started,
            done=len(done),
            failed=len(failed),
            rows_written=written,
            checkpoint={
                "params": params,
                "completed_codes": sorted(done),
                "checked_codes": sorted(done),
                "checked_at": today.isoformat(),
                "failed_codes": dict(sorted(failed.items())[:500]),
            },
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

REVENUE_BULK_YEARS = 5

REVENUE_CODE_CANDIDATES = ("股票代码", "代码", "code")

REVENUE_VALUE_CANDIDATES = (
    "营业总收入-营业总收入", "营业总收入", "营业收入", "revenue",
)

def _recent_quarter_ends(today: date, years: int = REVENUE_BULK_YEARS) -> list[date]:
    """``today`` 之前（含）的最近 ``years*4`` 个季度末，升序。"""
    count = max(1, int(years)) * 4
    ends: list[date] = []
    year = today.year
    while len(ends) < count and year >= 1990:
        for month, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
            period = date(year, month, day)
            if period <= today:
                ends.append(period)
                if len(ends) >= count:
                    break
        year -= 1
    return sorted(ends)

def _revenue_rows_from_report(
    raw: pd.DataFrame | None, period: date
) -> list[dict[str, Any]]:
    """东财业绩报表 → ``dim_stock_financial.revenue`` 更新行（绝对额，单位元）。"""
    if raw is None or len(raw) == 0:
        return []
    code_column = _resolve_column(raw.columns, REVENUE_CODE_CANDIDATES, ())
    value_column = _resolve_column(raw.columns, REVENUE_VALUE_CANDIDATES, ())
    if code_column is None or value_column is None:
        logger.warning(
            "业绩报表缺少代码/营收列（现有：%s），跳过 %s",
            [str(c) for c in list(raw.columns)[:10]],
            period,
        )
        return []
    values = _numeric_column(raw, value_column)
    if values is None:
        return []
    rows: dict[str, dict[str, Any]] = {}
    for code, value in zip(raw[code_column], values):
        if pd.isna(value):
            continue
        normalized = _normalize_code(code)
        if len(normalized) != 6 or not normalized.isdigit():
            continue
        rows[normalized] = {
            "code": normalized,
            "report_date": period,
            "revenue": round(float(value), 2),
        }
    return list(rows.values())

async def _periods_needing_revenue(
    db: "AsyncSession | None", periods: Sequence[date]
) -> list[date]:
    """筛出 revenue 缺失或明显不是绝对额的报告期（已修好的期不发请求）。"""
    target = sorted(set(periods))
    if not target:
        return []
    from sqlalchemy import case, func, select

    from app.db.models.dim import DimStockFinancial

    absolute_count = func.coalesce(
        func.sum(
            case((DimStockFinancial.revenue >= REVENUE_MIN_YUAN, 1), else_=0)
        ),
        0,
    )
    stmt = (
        select(
            DimStockFinancial.report_date,
            func.count(),
            absolute_count,
        )
        .where(DimStockFinancial.report_date.in_(target))
        .group_by(DimStockFinancial.report_date)
    )
    async with _db_scope(db) as session:
        stats = {
            row[0]: (int(row[1]), int(row[2]))
            for row in (await session.execute(stmt)).all()
        }
    need: list[date] = []
    for period in target:
        total, absolute = stats.get(period, (0, 0))
        if total == 0 or absolute / total < 0.5:
            need.append(period)
    return need

def _revenue_update_statement(rows: Sequence[dict[str, Any]]) -> Any:
    """``UPDATE dim_stock_financial SET revenue`` FROM (VALUES ...)，只更新已有行。"""
    from sqlalchemy import Date, Numeric, String, column, update, values

    from app.db.models.dim import DimStockFinancial

    table = DimStockFinancial.__table__
    rev_type = Numeric(20, 2)
    payload = values(
        column("code", String(6)),
        column("report_date", Date()),
        column("revenue", rev_type),
        name="v",
    ).data(
        [(row["code"], row["report_date"], row["revenue"]) for row in rows]
    )
    return (
        update(table)
        .where(
            table.c.code == payload.c.code,
            table.c.report_date == payload.c.report_date,
        )
        .values(revenue=payload.c.revenue)
    )

async def _bulk_update_revenue(
    db: "AsyncSession | None", rows: Sequence[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    total = 0
    async with _db_scope(db) as session:
        for chunk in chunked(list(rows), UPDATE_CHUNK_ROWS):
            result = await session.execute(_revenue_update_statement(chunk))
            affected = getattr(result, "rowcount", None)
            total += int(affected) if affected and affected > 0 else len(chunk)
    return total

async def sync_revenue_bulk(
    periods: Sequence[date] | None = None,
    db: "AsyncSession | None" = None,
    *,
    years: int = REVENUE_BULK_YEARS,
    force: bool = False,
) -> FetchResult:
    """阶段 1c：东财业绩报表批量同步绝对营业总收入（元）→ ``dim_stock_financial.revenue``。

    为什么需要它：新浪财报指标表**没有**绝对营收（只有「主营业务收入增长率」），
    而 PS = 总市值 / 营业总收入 TTM。逐只抓财务摘要要 5000+ 请求；业绩报表按报告期
    返回全市场，一个报告期一次请求（内部约 12 页），成本低两个数量级。

    只 UPDATE 已存在的财报行，不 INSERT。``force=False``（默认）时先检查该报告期已有
    值是否已是绝对额（``>= REVENUE_MIN_YUAN`` 占比 ≥ 50%），已修好的期直接跳过 ——
    所以日更通常零请求，季报窗口才真正拉取。

    Args:
        periods: 指定报告期；``None`` = 最近 ``years`` 年的季度末。
        db: 外部会话（只 flush）；``None`` 时自建事务。
        years: 默认报告期窗口（年）。
        force: 忽略"已是绝对额"检查，强制重拉。
    """
    started = time.perf_counter()
    target = (
        sorted({_as_date(value) for value in periods})
        if periods
        else _recent_quarter_ends(_today_cn(), years)
    )
    if not force:
        target = await _periods_needing_revenue(db, target)
    if not target:
        return FetchResult(
            rows=0,
            total=0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"task": "sync_revenue_bulk", "skipped": "revenue_fresh"},
        )

    written = 0
    failed: dict[str, str] = {}
    empty: list[str] = []
    for period in target:
        raw = await _ak_call("stock_yjbb_em", date=period.strftime("%Y%m%d"))
        rows = _revenue_rows_from_report(raw, period)
        if not rows:
            empty.append(period.isoformat())
            continue
        try:
            written += await _bulk_update_revenue(db, rows)
        except Exception as exc:  # noqa: BLE001 —— 单期失败不影响其他报告期
            failed[period.isoformat()] = f"{type(exc).__name__}: {exc}"
            logger.warning("营收批量更新失败 period=%s：%s", period, exc)

    elapsed = int((time.perf_counter() - started) * 1000)
    logger.info(
        "营收批量同步：%d 个报告期，写入 %d 行，空 %d，失败 %d",
        len(target), written, len(empty), len(failed),
    )
    return FetchResult(
        rows=written,
        total=len(target),
        failed=len(failed),
        elapsed_ms=elapsed,
        detail={
            "task": "sync_revenue_bulk",
            "source": "eastmoney",
            "periods": [period.isoformat() for period in target],
            "empty_periods": empty,
            "failed_periods": failed,
        },
    )

async def compute_valuation(
    start: date,
    end: date | None = None,
    codes: list[str] | None = None,
    db: "AsyncSession | None" = None,
) -> FetchResult:
    """阶段 2：用库里的财报/分红 + 库里的收盘价算估值，``UPDATE`` 回 ``fact_stock_daily``。

    **零网络**（不 import、不调用任何 akshare 接口）：输入全部来自
    ``dim_stock_financial``（阶段 1a）、``dim_stock_dividend``（阶段 1b）和
    ``fact_stock_daily.close``，所以可以每日跑、按区间反复跑，全市场单日秒级完成。

    因子口径：
        - ``pe_ttm = close / eps_ttm``、``pb = close / bps``：由
          :func:`build_valuation_frame` 按 ``announced_date`` 做 point-in-time 展开；
        - ``dv_ttm``：由 :func:`dv_ttm_series` 用**近 12 个月现金分红 / 收盘价**算
          （不再用 ``payout_ratio``，它语义是"分红/净利润"而不是股息率）。

    关键口径（point-in-time，防未来函数）：对每个交易日 ``d``，财报只用
    ``announced_date <= d`` 的最新报告期，分红只计 ``ex_date <= d`` 的方案。

    Args:
        start: 起始交易日（含）。
        end: 结束交易日（含）；``None`` = 只算 ``start`` 一天。
        codes: 指定代码；``None`` = 区间内有日线的全部股票。
        db: 外部 ``AsyncSession``（只 flush）；``None`` 时 ``session_scope()`` 自建事务。

    Returns:
        ``FetchResult``：``rows`` = 实际更新的日线行数，``total`` = 处理股票数，
        ``failed`` = 单只计算异常数。``detail`` 含 ``covered``/``no_financials`` 等。
    """
    started = time.perf_counter()
    start_date = _as_date(start)
    end_date = _as_date(end) if end is not None else start_date
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    code_list = await _codes_with_prices(db, start_date, end_date, codes)
    if not code_list:
        logger.info(
            "估值计算：%s~%s 区间内没有日线数据，跳过", start_date, end_date
        )
        return FetchResult(
            rows=0, total=0, elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={
                "skipped": "no_price_rows",
                "range": [start_date.isoformat(), end_date.isoformat()],
            },
        )

    if start_date == end_date:
        from app.ingest.valuation_daily import compute_valuation_single_day_fast

        return await compute_valuation_single_day_fast(
            code_list, start_date, db, started, dataset=DATASET
        )

    written = 0
    failed: dict[str, str] = {}
    no_financials: list[str] = []
    covered: set[str] = set()
    for batch in chunked(code_list, COMPUTE_BATCH_CODES):
        batch_codes = list(batch)
        financials = await _load_financials_for_codes(db, batch_codes)
        dividends = await _load_dividends_for_codes(db, batch_codes)
        prices = await _load_price_rows(db, batch_codes, start_date, end_date)
        if prices.empty:
            no_financials.extend(batch_codes)
            continue
        update_rows: list[dict[str, Any]] = []
        for code, price_group in prices.groupby("code", sort=False):
            code = str(code)
            code_financials = (
                financials[financials["code"] == code]
                if not financials.empty
                else financials
            )
            try:
                if code_financials.empty:
                    no_financials.append(code)
                    continue
                price_frame = price_group.drop(columns=["code"]).reset_index(drop=True)
                valuation = build_valuation_frame(price_frame, code_financials)
                # 股息率必须用"近 12 个月现金分红 / 股价"，覆盖 payout_ratio 口径
                code_dividends = (
                    dividends[dividends["code"] == code]
                    if not dividends.empty
                    else dividends
                )
                dv_frame = dv_ttm_series(price_frame, code_dividends)
                valuation = valuation.reset_index(drop=True)
                valuation["dv_ttm"] = dv_frame["dv_ttm"].to_numpy()
                valuation["ps_ttm"] = ps_ttm_values(
                    price_frame, valuation, code_financials
                ).to_numpy()
                code_rows = _valuation_update_rows(code, valuation)
            except Exception as exc:  # noqa: BLE001 —— 单只失败不影响整体
                failed[code] = f"{type(exc).__name__}: {exc}"
                logger.warning("估值计算失败 code=%s：%s", code, exc)
                continue
            if not code_rows:
                no_financials.append(code)
                continue
            update_rows.extend(code_rows)
            covered.add(code)
        if update_rows:
            written += await _bulk_update_valuation(db, update_rows)

    elapsed = int((time.perf_counter() - started) * 1000)
    coverage = len(covered) / len(code_list) if code_list else 0.0
    report = QualityReport(
        dataset=DATASET,
        rows=written,
        expected=len(code_list),
        coverage=round(coverage, 4),
    )
    if failed:
        report.add(
            "valuation_compute_failed",
            count=len(failed),
            severity="warning",
            message=f"{len(failed)} 只标的估值计算失败（未写库）",
            samples=sorted(failed)[:5],
        )
    if no_financials:
        report.add(
            "valuation_missing_financials",
            count=len(no_financials),
            severity="warning",
            message=(
                f"{len(no_financials)} 只标的没有可用财报（先跑 sync_financials 阶段）"
            ),
            samples=sorted(set(no_financials))[:5],
        )
    if coverage < config.QUALITY_MIN_VALUATION_COVERAGE:
        report.add(
            "valuation_coverage_below_threshold",
            count=len(no_financials),
            severity="error",
            message=(
                f"估值覆盖率 {coverage:.2%} < "
                f"{config.QUALITY_MIN_VALUATION_COVERAGE:.0%}（财报缓存缺失，先补 sync_financials）"
            ),
        )
    if covered:
        # 覆盖率不达标时记 stale，不能记 ok：否则监控看不到 PE/PB 大面积缺失
        await _record_quality(
            db, report, end_date,
            QualityStatus.OK if report.ok else QualityStatus.STALE,
        )
    else:
        # 一只都没算出来（例如财报缓存是空的）→ 记 error，前端能立刻看到原因
        await _record_quality(
            db,
            make_error_report(
                DATASET,
                f"{len(code_list)} 只标的都没有可用财报/价格数据（先跑 sync_financials）",
                rows=written,
            ),
            end_date,
            "error",
        )

    return FetchResult(
        rows=written,
        total=len(code_list),
        failed=len(failed),
        elapsed_ms=elapsed,
        detail={
            "task": "compute_valuation",
            "covered": len(covered),
            "no_financials": len(no_financials),
            "failed_codes": sorted(failed)[:100],
            "range": [start_date.isoformat(), end_date.isoformat()],
        },
        quality_status=(
            QualityStatus.OK.value if report.ok else QualityStatus.STALE.value
        ),
    )

async def backfill_valuation(
    codes: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    resume: bool = True,
    db: "AsyncSession | None" = None,
) -> FetchResult:
    """历史估值回补 = 阶段 1a（抓财报）+ 1b（抓分红）+ 阶段 2（算估值写回日线）。

    Args:
        codes: 指定代码；``None`` = 在市全部标的。
        start: 回补起始日期（默认 ``config.HISTORY_START``）。
        end: 结束日期（默认今天）。
        resume: 阶段 1a/1b 是否断点续传。
        db: 外部会话（传入只 flush）。注意：``app/pipeline.py`` 的 backfill 分支
            不传 db，即各阶段各自提交短事务，避免一个事务跨几十分钟。

    Returns:
        ``FetchResult``：``rows`` = 更新的日线行数，``total`` = 处理股票数，
        ``failed`` = 三个阶段的失败股票数之和。``detail`` 带各阶段摘要。
    """
    started = time.perf_counter()
    start_date = _as_date(start) if start is not None else config.HISTORY_START
    end_date = _as_date(end) if end is not None else _today_cn()
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    # TTM 需要"上年年报 + 上年同期"，回补起点往前留两年；同时不低于默认起始年
    start_year = min(DEFAULT_START_YEAR, start_date.year - 2)

    sync_result = await sync_financials(
        codes=codes,
        start_year=start_year,
        db=db,
        only_missing=True,
        resume=resume,
    )
    dividend_result = await sync_dividends(
        codes=codes, db=db, only_missing=True, resume=resume
    )
    revenue_result = await sync_revenue_bulk(
        _recent_quarter_ends(
            end_date, max(REVENUE_BULK_YEARS, end_date.year - start_year + 1)
        ),
        db=db,
    )
    compute_result = await compute_valuation(
        start_date, end_date, codes, db=db
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    total = (
        compute_result.total
        if (compute_result.total or 0) > 0
        else sync_result.total
    )
    return FetchResult(
        rows=compute_result.rows,
        total=total,
        failed=(
            sync_result.failed + dividend_result.failed
            + revenue_result.failed + compute_result.failed
        ),
        elapsed_ms=elapsed,
        detail={
            "task": "backfill_valuation",
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "start_year": start_year,
            "sync": sync_result.summary(),
            "dividends": dividend_result.summary(),
            "revenue": revenue_result.summary(),
            "compute": compute_result.summary(),
        },
        quality_status=compute_result.quality_status,
    )

async def _valuation_is_fresh(
    db: "AsyncSession | None",
    target: date,
) -> tuple[bool, int]:
    """估值是否已新鲜：价格、财报、分红三表在估值成功后都没有再更新。"""
    from sqlalchemy import text

    async with _db_scope(db) as session:
        row = (await session.execute(
            text(
                "SELECT last_trade_date, last_success_at, status, rows "
                "FROM meta_data_quality WHERE dataset = :dataset"
            ),
            {"dataset": DATASET},
        )).first()
        if row is None:
            return False, 0
        last_trade_date, last_success_at, status, existing = row
        if str(status) != QualityStatus.OK.value or last_trade_date != target:
            return False, int(existing or 0)
        if last_success_at is None:
            return False, int(existing or 0)

        stamps: list[datetime] = []
        for table in ("fact_stock_daily", "dim_stock_financial", "dim_stock_dividend"):
            where = " WHERE trade_date = :target" if table == "fact_stock_daily" else ""
            stamp = (await session.execute(
                text(f"SELECT max(updated_at) FROM {table}{where}"),
                {"target": target},
            )).scalar()
            if stamp is not None:
                stamps.append(stamp)
        if not stamps:
            return True, int(existing or 0)
        latest = max(
            stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)
            for stamp in stamps
        )
        success = (
            last_success_at
            if last_success_at.tzinfo is not None
            else last_success_at.replace(tzinfo=timezone.utc)
        )
        return success >= latest, int(existing or 0)


async def _collect_latest_valuation_impl(
    trade_date: date,
    db: "AsyncSession | None" = None,
    *,
    force: bool = False,
) -> FetchResult:
    """每日增量：只更新 ``trade_date`` 这一天 = 阶段 1a + 1b（``only_missing``）+ 阶段 2（单日）。

    正常情况下阶段 1a/1b 几乎零请求（财报已是最新可得报告期、分红 30 天窗口内不重抓），
    所以每日成本主要是阶段 2 的纯数据库计算。

    ``force=False`` 且三表输入未变时直接返回 ``skipped=already_collected``，
    防止 catch-up/重启把同一天的估值重复计算。

    Args:
        trade_date: 目标交易日。
        db: 外部会话（``pipeline`` 传入，只 flush 不 commit）。
        force: 忽略新鲜度判断，强制重算。

    Returns:
        ``FetchResult``：``rows`` = 更新的日线行数，``total`` = 当日有行情的股票数。
    """
    started = time.perf_counter()
    target = _as_date(trade_date)
    if not force and config.VALUATION_SKIP_IF_FRESH:
        fresh, existing = await _valuation_is_fresh(db, target)
        if fresh:
            logger.info("估值已新鲜，跳过 date=%s existing=%s", target, existing)
            return FetchResult(
                rows=0,
                total=0,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                quality_status=QualityStatus.OK.value,
                detail={
                    "task": "collect_latest_valuation",
                    "skipped": "already_collected",
                    "existing": existing,
                    "trade_date": target.isoformat(),
                },
            )
    sync_result = await sync_financials(
        codes=None, start_year=DEFAULT_START_YEAR, db=db, only_missing=True, resume=True
    )
    dividend_result = await sync_dividends(
        codes=None, db=db, only_missing=True, resume=True
    )
    revenue_result = await sync_revenue_bulk(db=db)
    compute_result = await compute_valuation(target, target, None, db=db)
    elapsed = int((time.perf_counter() - started) * 1000)
    if elapsed / 1000 > config.VALUATION_TARGET_SEC:
        logger.warning(
            "估值计算耗时 %.1fs 超过目标 %.0fs date=%s",
            elapsed / 1000, config.VALUATION_TARGET_SEC, target,
        )
    total = (
        compute_result.total
        if (compute_result.total or 0) > 0
        else sync_result.total
    )
    return FetchResult(
        rows=compute_result.rows,
        total=total,
        failed=(
            sync_result.failed + dividend_result.failed
            + revenue_result.failed + compute_result.failed
        ),
        elapsed_ms=elapsed,
        detail={
            "task": "collect_latest_valuation",
            "trade_date": target.isoformat(),
            "sync": sync_result.summary(),
            "dividends": dividend_result.summary(),
            "revenue": revenue_result.summary(),
            "compute": compute_result.summary(),
        },
        quality_status=compute_result.quality_status,
    )


async def collect_latest_valuation(
    trade_date: date,
    db: "AsyncSession | None" = None,
    *,
    force: bool = False,
) -> FetchResult:
    """每日估值入口：执行增量并在 ``ingest_job`` 记录整体状态与耗时。"""
    started = time.perf_counter()
    target = _as_date(trade_date)
    job_id = await _job_begin(
        db,
        "valuation",
        DATASET,
        {"trade_date": target.isoformat(), "force": force},
        None,
    )
    try:
        result = await _collect_latest_valuation_impl(trade_date, db, force=force)
        skipped = (result.detail or {}).get("skipped")
        status = (
            "skipped" if skipped == "already_collected"
            else ("success" if result.quality_status == QualityStatus.OK.value else "partial")
        )
        done = result.total or int((result.detail or {}).get("existing") or 0)
        await _job_finish(
            db, job_id, status,
            started_at=started,
            done=done,
            failed=result.failed,
            rows_written=result.rows,
        )
        return result
    except Exception as exc:  # noqa: BLE001 —— 先记失败 job，再交给上层
        await _job_finish(
            db, job_id, "failed",
            started_at=started,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


__all__ = [
    "REPORT_LAG_DAYS_BY_MONTH",
    "REPORT_LAG_FALLBACK_DAYS",
    "report_lag_days",
    "DIVIDEND_REFRESH_DAYS",
    "DV_WINDOW_DAYS",
    "DATASET",
    "FIN_COLUMNS",
    "TTM_COLUMNS",
    "VALUATION_COLUMNS",
    "DIVIDEND_COLUMNS",
    "normalize_financials",
    "eps_ttm_series",
    "revenue_ttm_series",
    "ps_ttm_values",
    "build_valuation_frame",
    "normalize_dividends",
    "dv_ttm_series",
    "normalize_abstract_metrics",
    "abstract_eps_frame",
    "fetch_financials",
    "sync_financials",
    "sync_revenue_bulk",
    "sync_dividends",
    "compute_valuation",
    "backfill_valuation",
    "collect_latest_valuation",
    "latest_knowable_report_period",
]
