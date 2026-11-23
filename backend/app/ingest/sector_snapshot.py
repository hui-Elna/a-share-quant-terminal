"""板块即时快照（V10）。

同花顺“即时”资金流一次返回行业 + 概念两张表，本模块只保留目标池，
写入 ``fact_sector_daily`` 的当日快照字段。

硬约束：
- 只允许当日执行；同花顺即时接口不能回溯历史；
- 专用 upsert：已存在行只更新快照字段，禁止用 NULL 覆盖历史 OHLCV/成交额/换手率；
- 新行 OHLCV/成交额/换手率一律 NULL，不用涨跌幅或其它字段冒充；
- 缺失值保留 NULL，不补 0。
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import config
from app.core.logging import get_logger
from app.core.status import QualityStatus
from app.db.models.fact import FactSectorDaily
from app.ingest.base import FetchResult
from app.ingest.quality import QualityReport, record_quality
from app.ingest.sector_pool import resolve_sector_pool
from app.ingest.ths_sector import build_sector_daily_rows, fetch_sector_fundflow, sector_net_inflow_map

logger = get_logger(__name__)

TASK = "sector_snapshot"
DATASET = "sector_snapshot"

#: 已存在行只更新这些快照字段；OHLCV/成交额/换手率永远不在这里。
_SNAPSHOT_UPDATE_COLUMNS = (
    "pct_chg",
    "up_count",
    "down_count",
    "leading_stock",
    "leading_stock_pct",
    "main_net_inflow",
    "src",
    "updated_at",
)


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return pd.to_datetime(value, errors="raise").date()


def _skipped(reason: str, trade_date: date, started: float) -> FetchResult:
    return FetchResult(
        rows=0,
        total=0,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        detail={"task": TASK, "skipped": reason, "trade_date": trade_date.isoformat()},
    )


def _snapshot_rows(
    frame: pd.DataFrame,
    pool_codes: set[str],
    trade_date: date,
) -> list[dict[str, Any]]:
    """过滤目标池并补齐快照字段；不修改单元格口径。"""
    if frame is None or frame.empty or "name" not in frame.columns:
        return []
    filtered = frame[
        frame["name"].map(
            lambda value: f"THS_{str(value).strip()}"[:24]
        ).isin(pool_codes)
    ].copy()
    if filtered.empty:
        return []
    rows = build_sector_daily_rows(filtered, {}, trade_date)
    inflow = sector_net_inflow_map(filtered)
    for row in rows:
        row["main_net_inflow"] = inflow.get(str(row.get("sector_name") or "").strip())
        row["src"] = "ths_snapshot"
        row["updated_at"] = datetime.now(timezone.utc)
        # 快照源没有 OHLCV/成交额/换手率：新行一律 NULL，不用 close 冒充。
        for column in (
            "open", "high", "low", "close", "pre_close", "amplitude",
            "volume", "amount", "turnover_rate",
        ):
            row[column] = None
    return rows


async def _upsert_snapshot_rows(session: Any, rows: list[dict[str, Any]]) -> int:
    """专用 upsert：只更新快照字段，保护历史 OHLCV。"""
    if not rows:
        return 0
    statement = pg_insert(FactSectorDaily).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[FactSectorDaily.sector_code, FactSectorDaily.trade_date],
        set_={
            column: (
                func.now()
                if column == "updated_at"
                else getattr(statement.excluded, column)
            )
            for column in _SNAPSHOT_UPDATE_COLUMNS
        },
    )
    result = await session.execute(statement)
    affected = getattr(result, "rowcount", None)
    return int(affected) if affected and affected > 0 else len(rows)


async def collect_sector_snapshot(
    trade_date: date,
    db: Any = None,
) -> FetchResult:
    """采集当日板块即时快照；历史日期直接跳过。"""
    started = time.perf_counter()
    day = _as_date(trade_date)

    # 延迟 import：避免 service 与 sector_snapshot 循环依赖。
    from app.ingest.service import (
        _JobTracker, _advisory_lock, _after_close_ready, _db_scope, _is_trade_day, _today_cn,
    )

    async with _advisory_lock(TASK) as acquired:
        if not acquired:
            return _skipped("lock_busy", day, started)
        job = _JobTracker(TASK, DATASET, trade_date=day)
        await job.begin()
        try:
            if not config.SECTOR_SNAPSHOT_ENABLED:
                await job.finish("skipped", error="disabled")
                return _skipped("disabled", day, started)
            if not await _is_trade_day(db, day):
                await job.finish("skipped", error="non_trade_day")
                return _skipped("non_trade_day", day, started)
            if day != _today_cn():
                await job.finish("skipped", error="only_today")
                return _skipped("only_today", day, started)
            if not _after_close_ready():
                await job.finish("skipped", error="before_close_ready_time")
                return _skipped("before_close_ready_time", day, started)

            pool = await resolve_sector_pool(db, day)
            if not pool.actionable_codes:
                await job.finish("skipped", error="empty_pool")
                return _skipped("empty_pool", day, started)
            await job.update(total=len(pool.actionable_codes))

            industry, concept = await asyncio.wait_for(
                fetch_sector_fundflow(),
                timeout=config.SECTOR_SNAPSHOT_TIMEOUT_SEC,
            )
            frame = pd.concat([industry, concept], ignore_index=True)
            rows = _snapshot_rows(frame, set(pool.actionable_codes), day)
            if not rows:
                await job.finish("failed", error="empty_rows")
                raise ValueError("板块即时快照为空（目标池无匹配行）")

            covered = {str(row["sector_code"]) for row in rows}
            expected = len(pool.actionable_codes)
            coverage = round(len(covered) / expected, 4) if expected else 0.0
            report = QualityReport(
                dataset=DATASET,
                rows=len(rows),
                expected=expected,
                coverage=coverage,
            )
            duplicates = len(rows) - len(covered)
            if duplicates:
                report.add(
                    "duplicate_rows", count=duplicates, severity="error",
                    message=f"{duplicates} 个重复板块代码",
                )
            if coverage < config.QUALITY_MIN_COVERAGE:
                report.add(
                    "coverage_below_threshold", count=len(covered),
                    severity="error",
                    message=(
                        f"板块快照覆盖率 {coverage:.2%} < "
                        f"{config.QUALITY_MIN_COVERAGE:.0%}"
                    ),
                )
            status = QualityStatus.OK if report.ok else QualityStatus.QUALITY_FAILED
            async with _db_scope(db) as session:
                written = await _upsert_snapshot_rows(session, rows)
                await record_quality(session, DATASET, report, day, status)

            elapsed = int((time.perf_counter() - started) * 1000)
            await job.finish(
                "success" if status is QualityStatus.OK else "partial",
                done=len(covered),
                failed=max(0, expected - len(covered)),
                rows_written=written,
            )
            return FetchResult(
                rows=written,
                total=expected,
                elapsed_ms=elapsed,
                quality_status=status.value,
                detail={
                    "task": TASK,
                    "coverage": coverage,
                    "target_boards": expected,
                    "covered": len(covered),
                    "pool_version": pool.version,
                    "src": "ths_snapshot",
                },
            )
        except Exception as exc:  # noqa: BLE001 —— 失败记录后交给 run_task 统一处理
            await job.finish("failed", error=f"{type(exc).__name__}: {exc}")
            raise


__all__ = ["collect_sector_snapshot"]
