"""盘后串行链路：数据采集 → 仓位罗盘分析 → 机器人推送。

背景
----
宿主机无法 7×24 小时运行，固定时点的 APScheduler 任务会整体错过。本模块把
原先 16:10 / 16:20 / 16:30 / 17:00 的独立任务合并为一条**串行、可恢复**的链路：

1. **data**：`run_daily_pipeline` 完成后，逐项校验 `meta_data_quality` 与当日
   `stg_run`；任一必需数据集或启用策略未就绪即视为失败，后续环节不执行。
2. **compass**：ETF 同步（失败不阻断）→ `generate_card` → 校验卡片核心字段。
3. **push**：选股播报 → 罗盘卡片推送（两者都尝试，任一失败记为失败）。

触发方式
--------
- 15:36 定时流水线（机会触发，进程在跑时立即执行）；
- 启动补跑 `catch_up` 完成后恢复未完成环节；
- 盘后巡检（每 30 分钟）重试失败环节；
- `POST /api/v1/admin/daily-chain` 手动触发。

并发与状态
----------
整条链路持有 `gfna:daily_chain` advisory lock；每次运行写一条
`ingest_job(task="daily_chain")`，任务监控页可直接看到阶段状态。
链路本身幂等：数据已就绪则跳过 data，卡片已存在则跳过罗盘生成，推送按
`push_log` / `compass_push_log` 去重，重复执行不会重复发送。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import config
from app.core.logging import bind_run, get_logger
from app.core.status import QualityStatus, TaskStatus
from app.db.models import (
    FactDecisionCard, FactEtfDaily, IngestJob, MetaDataQuality, StgRun,
)
from app.pipeline import advisory_unlock, run_daily_pipeline, try_advisory_lock
from app.strategy.strategies import SPECS

logger = get_logger("daily_chain")

_TZ = ZoneInfo(config.TIMEZONE)
LOCK = "gfna:daily_chain"
CHAIN_TASK = "daily_chain"
#: 罗盘与推送共同依赖的最小数据集闭环（dataset, pipeline step）
REQUIRED_DATASETS: tuple[tuple[str, str], ...] = (
    ("stock_daily", "snapshot"),
    ("stock_features", "features"),
    ("stock_scores", "scores"),
    ("sector_scores", "sector_scores"),
    ("market_temperature", "temperature"),
    ("market_regime", "regime"),
    ("algo_run", "algos"),
)
_CARD_VALID_STATUS = {"generated", "published"}


class ChainStageError(RuntimeError):
    """某一环节未通过；后续环节必须停止。"""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        blocked: bool = False,
    ) -> None:
        self.stage = stage
        self.detail = detail or {}
        self.blocked = blocked
        super().__init__(message)


# ============================================================
# 时间与目标交易日
# ============================================================

def _now_cn() -> datetime:
    return datetime.now(_TZ)


def _after_close(now: datetime | None = None) -> bool:
    moment = now or _now_cn()
    return (moment.hour, moment.minute) >= (15, 30)


async def resolve_chain_date(db: AsyncSession) -> date | None:
    """返回当前应推进的交易日：收盘后取今天（开市日），否则取上一开市日。"""
    from app.ingest.calendar import is_trade_day, latest_trade_date

    now = _now_cn()
    today = now.date()
    if _after_close(now) and await is_trade_day(db, today):
        return today
    cutoff = today if _after_close(now) else today - timedelta(days=1)
    return await latest_trade_date(db, on_or_before=cutoff)


def expected_push_algos() -> list[str]:
    """当前应产出当日 run 的主策略；未配置时退回全部启用策略。"""
    configured = [code for code in config.PUSH_ALGOS if code in SPECS]
    return configured or list(SPECS)


# ============================================================
# 就绪校验
# ============================================================

async def data_readiness(
    db: AsyncSession, trade_date: date
) -> tuple[bool, dict[str, Any]]:
    """校验数据阶段是否真正完成：质量表 + 当日主策略 run 双重确认。"""
    names = [dataset for dataset, _ in REQUIRED_DATASETS]
    rows = (
        await db.execute(
            select(MetaDataQuality).where(MetaDataQuality.dataset.in_(names))
        )
    ).scalars().all()
    quality = {row.dataset: row for row in rows}

    missing_datasets: list[dict[str, Any]] = []
    for dataset, step in REQUIRED_DATASETS:
        row = quality.get(dataset)
        ok = (
            row is not None
            and str(row.status) == QualityStatus.OK.value
            and row.last_trade_date == trade_date
        )
        if not ok:
            missing_datasets.append({
                "dataset": dataset,
                "step": step,
                "status": str(row.status) if row is not None else None,
                "last_trade_date": (
                    row.last_trade_date.isoformat()
                    if row is not None and row.last_trade_date else None
                ),
            })

    algos = expected_push_algos()
    runs = set(
        (
            await db.execute(
                select(StgRun.algo_code).where(
                    StgRun.trade_date == trade_date,
                    StgRun.is_backtest.is_(False),
                    StgRun.status == TaskStatus.SUCCESS.value,
                    StgRun.algo_code.in_(algos),
                )
            )
        ).scalars().all()
    )
    missing_algos = sorted(set(algos) - runs)
    detail = {
        "trade_date": trade_date.isoformat(),
        "missing_datasets": missing_datasets,
        "missing_algos": missing_algos,
        "expected_algos": algos,
    }
    return (not missing_datasets and not missing_algos), detail


async def find_card(
    db: AsyncSession, trade_date: date
) -> FactDecisionCard | None:
    return (
        await db.execute(
            select(FactDecisionCard)
            .where(FactDecisionCard.trade_date == trade_date)
            .order_by(FactDecisionCard.generated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def validate_card(card: FactDecisionCard | None, trade_date: date) -> dict[str, Any]:
    """只接受字段完整、可被推送的卡片；空仓卡也是有效结果。"""
    if card is None:
        return {"ok": False, "reason": "当日无决策卡"}
    if card.trade_date != trade_date:
        return {"ok": False, "reason": "卡片交易日不匹配"}
    if str(card.status) not in _CARD_VALID_STATUS:
        return {"ok": False, "reason": f"卡片状态不可用: {card.status}"}
    if card.exec_cap is None:
        return {"ok": False, "reason": "卡片缺少执行仓位上限"}
    if not isinstance(card.candidates, list):
        return {"ok": False, "reason": "卡片候选池结构异常"}
    return {
        "ok": True,
        "card_id": card.card_id,
        "exec_cap": card.exec_cap,
        "candidates": len(card.candidates),
        "card_status": card.status,
    }


async def failed_data_attempts(db: AsyncSession, trade_date: date) -> int:
    """当日以 run_data=True 失败在 data 阶段的链路运行次数（用于限量重采）。"""
    result = await db.execute(
        select(func.count()).select_from(IngestJob).where(
            IngestJob.task == CHAIN_TASK,
            IngestJob.trade_date == trade_date,
            IngestJob.status == TaskStatus.FAILED.value,
            IngestJob.checkpoint["failed_stage"].astext == "data",
        )
    )
    return int(result.scalar() or 0)


async def should_retry_data(db: AsyncSession, trade_date: date) -> bool:
    """是否还允许再重采一次：初始尝试之外，最多重试 N 次。"""
    max_retries = max(0, int(config.DAILY_CHAIN_MAX_DATA_RETRIES))
    if max_retries == 0:
        return False
    return await failed_data_attempts(db, trade_date) < 1 + max_retries


# ============================================================
# 阶段执行
# ============================================================

def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def _run_data_stage(
    db: AsyncSession, trade_date: date, *, run_data: bool
) -> dict[str, Any]:
    started = time.perf_counter()
    ready, readiness = await data_readiness(db, trade_date)
    if ready:
        return {
            "status": TaskStatus.SKIPPED.value,
            "reason": "already_ready",
            "readiness": readiness,
            "elapsed_ms": _elapsed_ms(started),
        }
    if not run_data:
        raise ChainStageError(
            "data",
            "数据尚未采集完成，链路等待下一次巡检",
            detail=readiness,
            blocked=True,
        )

    # 恢复场景只补缺：已新鲜的高成本步骤（估值/板块/资金流）不再重复拉取。
    pipeline = await run_daily_pipeline(trade_date, db, skip_if_fresh=True)
    steps = pipeline.get("steps") or {}
    # 只把罗盘/推送真正依赖的步骤纳入门禁：两融 T+1 披露、北向为空等
    # 非必需步骤失败不应阻断盘后链路（否则每个交易日都会卡在 margin）。
    required_steps = {step for _, step in REQUIRED_DATASETS}
    failed_steps = {
        name: value.get("error") or value.get("status")
        for name, value in steps.items()
        if name in required_steps
        and isinstance(value, dict)
        and value.get("status") == TaskStatus.FAILED.value
    }
    partial_steps = {
        name: value.get("error") or value.get("status")
        for name, value in steps.items()
        if name in required_steps
        and isinstance(value, dict)
        and value.get("status") == TaskStatus.PARTIAL.value
    }
    ready, readiness = await data_readiness(db, trade_date)
    if not ready or failed_steps or partial_steps:
        raise ChainStageError(
            "data",
            "数据采集或入库校验未通过",
            detail={
                "readiness": readiness,
                "failed_steps": failed_steps,
                "partial_steps": partial_steps,
            },
        )
    return {
        "status": TaskStatus.SUCCESS.value,
        "steps": len(steps),
        "readiness": readiness,
        "elapsed_ms": _elapsed_ms(started),
    }


async def _run_compass_stage(
    db: AsyncSession, trade_date: date
) -> dict[str, Any]:
    started = time.perf_counter()
    existing = await find_card(db, trade_date)
    validation = validate_card(existing, trade_date)
    if validation["ok"]:
        return {
            "status": TaskStatus.SKIPPED.value,
            "reason": "already_generated",
            **validation,
            "elapsed_ms": _elapsed_ms(started),
        }

    etf: dict[str, Any]
    etf_exists = (await db.execute(
        select(FactEtfDaily.etf_code)
        .where(FactEtfDaily.trade_date == trade_date)
        .limit(1)
    )).scalar() is not None
    if etf_exists:
        etf = {"status": "skipped", "reason": "already_synced"}
    else:
        try:
            from app.compass.etf_sync import sync_etf_data

            etf = await sync_etf_data(db, trade_date, mode="auto")
        except Exception as exc:  # noqa: BLE001 - ETF 缺失不阻断卡片生成
            await db.rollback()
            etf = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            logger.warning("ETF 同步失败（不阻断罗盘）: %s", type(exc).__name__)

    try:
        from app.compass.service import generate_card

        card = await generate_card(db, trade_date)
    except Exception as exc:  # noqa: BLE001 - 统一转成阶段失败
        raise ChainStageError(
            "compass",
            f"仓位罗盘生成失败: {type(exc).__name__}: {exc}",
            detail={"etf": etf},
        ) from exc

    validation = validate_card(card, trade_date)
    if not validation["ok"]:
        raise ChainStageError(
            "compass",
            f"仓位罗盘结果无效: {validation.get('reason')}",
            detail={"validation": validation, "etf": etf},
        )
    return {
        "status": TaskStatus.SUCCESS.value,
        **validation,
        "etf": etf,
        "elapsed_ms": _elapsed_ms(started),
    }


async def _run_push_stage(
    db: AsyncSession, trade_date: date, card_id: str | None
) -> dict[str, Any]:
    started = time.perf_counter()
    errors: list[str] = []

    if config.PUSH_OBSERVATION:
        try:
            from app.worker import run_observation_algos

            await run_observation_algos(db)
        except Exception:  # noqa: BLE001 - 观察候选失败不阻断主推送
            logger.exception("观察候选运行失败（不阻断推送）")

    try:
        from app.notify.push import push_daily_results

        stock = await push_daily_results(db, trade_date)
    except Exception as exc:  # noqa: BLE001 - 推送约定不抛异常，这里兜底
        stock = {"error": f"{type(exc).__name__}: {exc}"}
    if stock.get("error"):
        errors.append(f"选股推送失败: {stock['error']}")

    compass: dict[str, Any] = {"status": "skipped", "reason": "无卡片"}
    if card_id:
        try:
            from app.compass.push import send_card_push

            compass = await send_card_push(db, card_id)
        except Exception as exc:  # noqa: BLE001
            compass = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        if compass.get("status") in {"failed", "not_found"}:
            errors.append(f"罗盘推送失败: {compass.get('error') or compass.get('status')}")

    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        raise ChainStageError(
            "push",
            f"推送结果落库失败: {type(exc).__name__}: {exc}",
            detail={"stock": stock, "compass": compass},
        ) from exc

    result = {
        "status": TaskStatus.SUCCESS.value if not errors else TaskStatus.FAILED.value,
        "stock": stock,
        "compass": compass,
        "elapsed_ms": _elapsed_ms(started),
    }
    if errors:
        raise ChainStageError(
            "push", "；".join(errors), detail=result
        )
    return result


# ============================================================
# 任务状态（ingest_job）
# ============================================================

async def _start_job(
    db: AsyncSession, trade_date: date, reason: str, run_data: bool
) -> IngestJob:
    now = datetime.now(timezone.utc)
    await db.execute(
        update(IngestJob)
        .where(
            IngestJob.task == CHAIN_TASK,
            IngestJob.status == TaskStatus.RUNNING.value,
        )
        .values(
            status=TaskStatus.FAILED.value,
            finished_at=now,
            error="被新的链路运行取代",
        )
    )
    job = IngestJob(
        job_id=str(uuid4()),
        task=CHAIN_TASK,
        dataset=CHAIN_TASK,
        trade_date=trade_date,
        params={"reason": reason, "run_data": run_data},
        status=TaskStatus.RUNNING.value,
        started_at=now,
        checkpoint={"stages": {}},
    )
    db.add(job)
    await db.commit()
    return job


async def _finish_job(
    db: AsyncSession, job: IngestJob | None, summary: dict[str, Any]
) -> None:
    if job is None:
        return
    try:
        now = datetime.now(timezone.utc)
        job.status = summary.get("job_status") or summary.get("status") or "failed"
        job.error = summary.get("error")
        job.checkpoint = {
            "stages": summary.get("stages") or {},
            "failed_stage": summary.get("failed_stage"),
        }
        job.finished_at = now
        job.elapsed_ms = int(
            (now - job.started_at).total_seconds() * 1000
        ) if job.started_at else None
        await db.commit()
    except Exception:  # noqa: BLE001 - 状态落库失败不能掩盖链路结果
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.exception("链路状态落库失败 job=%s", getattr(job, "job_id", None))


# ============================================================
# 主入口
# ============================================================

async def run_daily_chain(
    db: AsyncSession | None = None,
    trade_date: date | None = None,
    *,
    run_data: bool = False,
    reason: str = "manual",
    notify_on_blocked: bool = False,
) -> dict[str, Any]:
    """串行推进当日链路；任一步失败立即停止后续环节。永不抛异常。"""
    if db is None:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as own_db:
            return await run_daily_chain(
                own_db,
                trade_date,
                run_data=run_data,
                reason=reason,
                notify_on_blocked=notify_on_blocked,
            )

    try:
        target = trade_date or await resolve_chain_date(db)
    except Exception as exc:  # noqa: BLE001 - DB 不可用时不让调度器崩溃
        logger.exception("解析链路交易日失败")
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if target is None:
        return {"status": "skipped", "reason": "no_trade_date"}

    from app.ingest.calendar import is_trade_day

    try:
        trade_day = await is_trade_day(db, target)
    except Exception as exc:  # noqa: BLE001
        logger.exception("判断交易日失败")
        return {"status": "failed", "trade_date": target.isoformat(),
                "error": f"{type(exc).__name__}: {exc}"}
    if not trade_day:
        return {"status": "skipped", "reason": "not_trade_day",
                "trade_date": target.isoformat()}

    try:
        acquired = await try_advisory_lock(db, LOCK)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取链路锁失败")
        return {"status": "failed", "trade_date": target.isoformat(),
                "error": f"{type(exc).__name__}: {exc}"}
    if not acquired:
        return {"status": "skipped", "reason": "lock_busy",
                "trade_date": target.isoformat()}

    summary: dict[str, Any] = {
        "trade_date": target.isoformat(),
        "status": "running",
        "reason": reason,
        "run_data": run_data,
        "stages": {},
    }
    job: IngestJob | None = None
    stage = "data"
    try:
        with bind_run(task=CHAIN_TASK, trade_date=target.isoformat()):
            logger.info("盘后链路开始 reason=%s run_data=%s", reason, run_data)
            job = await _start_job(db, target, reason, run_data)

            summary["stages"]["data"] = await _run_data_stage(
                db, target, run_data=run_data
            )
            stage = "compass"
            summary["stages"]["compass"] = await _run_compass_stage(db, target)
            stage = "push"
            summary["stages"]["push"] = await _run_push_stage(
                db, target, summary["stages"]["compass"].get("card_id")
            )
            summary["status"] = TaskStatus.SUCCESS.value
            logger.info("盘后链路完成 trade_date=%s", target)
    except ChainStageError as exc:
        summary["failed_stage"] = exc.stage
        summary["error"] = str(exc)
        summary["detail"] = exc.detail
        summary["stages"][exc.stage] = {
            "status": "blocked" if exc.blocked else TaskStatus.FAILED.value,
            "error": str(exc),
            "detail": exc.detail,
        }
        if exc.blocked:
            summary["status"] = "blocked"
            summary["job_status"] = TaskStatus.SKIPPED.value
            logger.info("盘后链路等待数据：%s", exc)
        else:
            summary["status"] = TaskStatus.FAILED.value
            summary["job_status"] = TaskStatus.FAILED.value
            logger.error("盘后链路失败 stage=%s: %s", exc.stage, exc)
            await _alert(db, target, exc)
    except Exception as exc:  # noqa: BLE001 - 兜底，避免定时任务崩溃
        summary["failed_stage"] = stage
        summary["status"] = TaskStatus.FAILED.value
        summary["job_status"] = TaskStatus.FAILED.value
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["stages"][stage] = {
            "status": TaskStatus.FAILED.value,
            "error": summary["error"],
        }
        logger.exception("盘后链路异常 stage=%s", stage)
        await _alert(
            db, target, ChainStageError(stage, summary["error"])
        )
    finally:
        await _finish_job(db, job, summary)
        try:
            await advisory_unlock(db, LOCK)
        except Exception:  # noqa: BLE001 - 连接已断时锁会随会话释放
            logger.warning("释放链路锁失败（会话可能已断开）", exc_info=True)

    if summary["status"] == "blocked" and notify_on_blocked:
        await _alert(
            db,
            target,
            ChainStageError(
                summary.get("failed_stage") or "data",
                summary.get("error") or "数据未就绪",
                detail=summary.get("detail"),
            ),
        )
    return summary


async def _alert(
    db: AsyncSession, trade_date: date, exc: ChainStageError
) -> None:
    from app.notify.alerts import STAGE_LABELS, send_chain_alert

    blocked_map = {
        "data": [STAGE_LABELS["compass"], STAGE_LABELS["push"]],
        "compass": [STAGE_LABELS["push"]],
        "push": [],
    }
    try:
        await send_chain_alert(
            db,
            trade_date,
            exc.stage,
            str(exc),
            blocked=blocked_map.get(exc.stage),
            detail=exc.detail,
        )
    except Exception:  # noqa: BLE001 - 告警失败不影响链路
        logger.exception("链路告警发送异常 stage=%s", exc.stage)


async def run_daily_chain_background(
    trade_date: date | None = None,
    *,
    run_data: bool = False,
    reason: str = "manual",
) -> None:
    """后台入口：自建会话，异常只记日志。"""
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            result = await run_daily_chain(
                db, trade_date, run_data=run_data, reason=reason
            )
            logger.info("后台链路结束 status=%s stages=%s",
                        result.get("status"), list((result.get("stages") or {}).keys()))
        except Exception:  # noqa: BLE001
            logger.exception("后台链路执行失败")


__all__ = [
    "LOCK", "CHAIN_TASK", "REQUIRED_DATASETS", "ChainStageError",
    "resolve_chain_date", "expected_push_algos", "data_readiness",
    "find_card", "validate_card", "failed_data_attempts", "should_retry_data",
    "run_daily_chain", "run_daily_chain_background",
]
