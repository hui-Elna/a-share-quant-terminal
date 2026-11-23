"""单日估值快速路径（V10 从 valuation.py 拆出，控制模块体积）。"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from app.core import config
from app.core.status import QualityStatus
from app.ingest.base import FetchResult
from app.ingest.quality import QualityReport
from app.ingest.valuation_math import (
    DV_WINDOW_DAYS,
    REPORT_LAG_DAYS_BY_MONTH,
    REPORT_LAG_FALLBACK_DAYS,
    REVENUE_MIN_YUAN,
)
from app.ingest.valuation_store import (
    _as_db_value,
    _bulk_update_valuation,
    _load_dividends_for_codes,
    _load_financials_for_codes,
    _load_price_rows,
    _record_quality,
)

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

async def compute_valuation_single_day_fast(
    code_list: list[str],
    target: date,
    db: "AsyncSession | None",
    started: float,
    *,
    dataset: str = "valuation",
) -> FetchResult:
    """单日估值快速路径：一次批量取数 + 向量化计算 + 一次批量 UPDATE。

    与 ``build_valuation_frame`` / ``dv_ttm_series`` / ``ps_ttm_values`` 保持同一口径：
    - 最新可用报告期：``announced_date <= target``，缺失/脏 announced_date 回落到报告期 + 法定滞后；
    - ``eps_ttm`` / ``bps`` 直接取 ``dim_stock_financial``；
    - ``revenue_ttm`` 用同一套“本期 + 上年年报 − 上年同期”公式；
    - 股息率 = 近 365 天 dps 合计 / close × 100，无分红为 0，无价格为 NULL；
    - PS = market_cap / revenue_ttm，仅保留 market_cap > 0 且 |revenue_ttm| >= REVENUE_MIN_YUAN。
    """
    prices = await _load_price_rows(db, code_list, target, target)
    financials = await _load_financials_for_codes(db, code_list)
    dividends = await _load_dividends_for_codes(db, code_list)

    failed: dict[str, str] = {}
    no_financials: list[str] = []
    covered: set[str] = set()

    if prices.empty:
        no_financials = list(code_list)
        rows: list[dict[str, Any]] = []
    else:
        prices = prices.copy()
        prices["code"] = prices["code"].astype(str)
        prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
        prices["market_cap"] = pd.to_numeric(prices["market_cap"], errors="coerce")
        prices = prices.drop_duplicates("code", keep="last")

        if financials.empty:
            latest = pd.DataFrame(columns=["code"])
            no_financials = list(code_list)
        else:
            fins = financials.copy()
            fins["code"] = fins["code"].astype(str)
            fins["report_date"] = pd.to_datetime(fins["report_date"], errors="coerce")
            fins["announced_date"] = pd.to_datetime(
                fins["announced_date"].astype(object), errors="coerce"
            )
            fins["eps_ttm"] = pd.to_numeric(fins["eps_ttm"], errors="coerce")
            fins["bps"] = pd.to_numeric(fins["bps"], errors="coerce")
            fins["revenue"] = pd.to_numeric(fins["revenue"], errors="coerce")
            fins = fins.dropna(subset=["report_date"])

            period = fins["report_date"]
            lag = period.dt.month.map(REPORT_LAG_DAYS_BY_MONTH).fillna(
                REPORT_LAG_FALLBACK_DAYS
            ).astype(int)
            fallback = period + pd.to_timedelta(lag, unit="D")
            announced = fins["announced_date"]
            available = announced.fillna(fallback)
            invalid = announced.notna() & (announced < period)
            fins["available_date"] = available.where(~invalid, fallback)
            fins = fins.dropna(subset=["available_date"])
            fins = fins[fins["available_date"] <= pd.Timestamp(target)]
            fins = fins.sort_values(
                ["code", "available_date", "report_date"], kind="mergesort"
            )
            latest = fins.drop_duplicates("code", keep="last")

            # revenue TTM：同一套三期公式，向量化 merge。
            revenue_rows = financials[["code", "report_date", "revenue"]].copy()
            revenue_rows["code"] = revenue_rows["code"].astype(str)
            revenue_rows["report_date"] = pd.to_datetime(
                revenue_rows["report_date"], errors="coerce"
            )
            revenue_rows["revenue"] = pd.to_numeric(
                revenue_rows["revenue"], errors="coerce"
            )
            revenue_rows = revenue_rows.dropna(subset=["report_date"])
            revenue_rows = revenue_rows.drop_duplicates(
                ["code", "report_date"], keep="last"
            )
            latest = latest.merge(
                revenue_rows.rename(columns={"revenue": "rev_current"}),
                on=["code", "report_date"], how="left",
            )
            latest["prior_annual"] = latest["report_date"].map(
                lambda value: pd.Timestamp(date(value.year - 1, 12, 31))
            )
            latest["prior_same"] = latest["report_date"].map(
                lambda value: pd.Timestamp(date(value.year - 1, value.month, value.day))
            )
            latest = latest.merge(
                revenue_rows.rename(columns={
                    "report_date": "prior_annual", "revenue": "rev_prev_annual",
                }),
                on=["code", "prior_annual"], how="left",
            )
            latest = latest.merge(
                revenue_rows.rename(columns={
                    "report_date": "prior_same", "revenue": "rev_prev_same",
                }),
                on=["code", "prior_same"], how="left",
            )
            is_annual = latest["report_date"].map(
                lambda value: value.month == 12 and value.day == 31
            )
            latest["revenue_ttm"] = np.where(
                is_annual,
                latest["rev_current"],
                latest["rev_current"] - latest["rev_prev_same"] + latest["rev_prev_annual"],
            )

        merged = prices.merge(
            latest[["code", "eps_ttm", "bps", "revenue_ttm"]],
            on="code", how="left",
        ) if "eps_ttm" in latest.columns else prices.copy()
        if "eps_ttm" not in merged.columns:
            merged["eps_ttm"] = np.nan
            merged["bps"] = np.nan
            merged["revenue_ttm"] = np.nan

        if dividends.empty:
            dps_sum = pd.Series(dtype="float64")
        else:
            div = dividends.copy()
            div["code"] = div["code"].astype(str)
            div["ex_date"] = pd.to_datetime(div["ex_date"], errors="coerce").dt.date
            div["dps"] = pd.to_numeric(div["dps"], errors="coerce")
            div = div[div["dps"].notna() & (div["dps"] > 0)]
            window_start = target - timedelta(days=DV_WINDOW_DAYS)
            div = div[
                (div["ex_date"] > window_start) & (div["ex_date"] <= target)
            ]
            # 与 _prepare_dividends 一致：先按 (code, ex_date) 合并同日多笔，再按 code 汇总。
            dps_sum = (
                div.groupby(["code", "ex_date"], as_index=False)["dps"].sum()
                .groupby("code")["dps"].sum()
            )
        merged = merged.merge(
            dps_sum.rename("dps_sum"), left_on="code", right_index=True, how="left"
        )
        merged["dps_sum"] = merged["dps_sum"].fillna(0.0)

        close = merged["close"]
        eps = pd.to_numeric(merged["eps_ttm"], errors="coerce")
        bps = pd.to_numeric(merged["bps"], errors="coerce")
        market_cap = merged["market_cap"]
        revenue = pd.to_numeric(merged["revenue_ttm"], errors="coerce")
        pe = close / eps
        pb = close / bps
        ps = market_cap / revenue
        dv = merged["dps_sum"] / close * 100.0
        merged["pe_ttm"] = pe.where(close.notna() & eps.notna() & (eps != 0))
        merged["pb"] = pb.where(close.notna() & bps.notna() & (bps > 0))
        merged["ps_ttm"] = ps.where(
            market_cap.notna() & (market_cap > 0)
            & revenue.notna() & (revenue.abs() >= REVENUE_MIN_YUAN)
        )
        merged["dv_ttm"] = dv.where(close.notna() & (close > 0))

        rows = []
        for record in merged.to_dict("records"):
            pe_value = _as_db_value(record.get("pe_ttm"), 4)
            pb_value = _as_db_value(record.get("pb"), 4)
            dv_value = _as_db_value(record.get("dv_ttm"), 4)
            ps_value = _as_db_value(record.get("ps_ttm"), 4)
            if pe_value is None and pb_value is None and dv_value is None and ps_value is None:
                continue
            covered.add(str(record["code"]))
            rows.append({
                "code": str(record["code"]),
                "trade_date": target,
                "pe_ttm": pe_value,
                "pb": pb_value,
                "dv_ttm": dv_value,
                "ps_ttm": ps_value,
            })

    written = await _bulk_update_valuation(db, rows)
    coverage = len(covered) / len(code_list) if code_list else 0.0
    report = QualityReport(
        dataset=dataset, rows=written, expected=len(code_list), coverage=round(coverage, 4)
    )
    if no_financials:
        report.add(
            "valuation_missing_financials", count=len(no_financials),
            severity="warning",
            message=f"{len(no_financials)} 只标的没有可用财报",
            samples=sorted(set(no_financials))[:5],
        )
    if failed:
        report.add(
            "valuation_compute_failed", count=len(failed), severity="warning",
            message=f"{len(failed)} 只标的估值计算失败",
        )
    if coverage < config.QUALITY_MIN_VALUATION_COVERAGE:
        report.add(
            "valuation_coverage_below_threshold", count=len(no_financials),
            severity="error",
            message=(
                f"估值覆盖率 {coverage:.2%} < "
                f"{config.QUALITY_MIN_VALUATION_COVERAGE:.0%}"
            ),
        )
    await _record_quality(
        db, report, target,
        QualityStatus.OK if report.ok else QualityStatus.STALE,
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    return FetchResult(
        rows=written,
        total=len(code_list),
        failed=len(failed),
        elapsed_ms=elapsed,
        detail={
            "task": "compute_valuation",
            "mode": "single_day_fast",
            "covered": len(covered),
            "no_financials": len(no_financials),
            "failed_codes": sorted(failed)[:100],
            "range": [target.isoformat(), target.isoformat()],
        },
    )
