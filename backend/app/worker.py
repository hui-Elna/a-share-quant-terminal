"""
定时任务（方案 A8 / F2）
========================
单进程 APScheduler + 数据库 advisory lock：
  - 15:36 盘后串行链路（数据 → 仓位罗盘 → 机器人推送；真实交易日判断）
  - 15:05–23:35 每 30 分钟巡检链路，按额度补采数据并续跑罗盘/推送
  - 16:10 起每 2 小时数据库备份（当天已成功则跳过）
  - 17:10 信号前瞻统计（依赖当日选股结果）
  - 17:30 纸面组合（水位 + 60 日重叠窗口，每周全量校准；非交易日跳过）
  - 02:00 历史补齐（断点续传，夜间执行）
  - 每 N 分钟新闻采集（作为情绪因子输入）

**为什么不用 Celery**：方案 F2 明确"先单进程 + DB 锁，量大再上队列"。
`pg_try_advisory_lock` 保证多 worker 部署时同一时刻只有一个实例在跑，定时任务因此幂等。

**进程不常驻时**：固定时点只作为机会触发；服务启动后由 `catch_up` 补齐数据并
恢复未完成的罗盘/推送环节，盘后巡检继续重试，因此不依赖进程恰好 16:20/17:00 在线。
"""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import (
    CRAWL_INTERVAL_MINUTES,
    MONEYFLOW_FULL_WEEKLY,
    SECTOR_MEMBERS_WEEKLY,
    TIMEZONE,
)
from app.core.logging import bind_run, get_logger

logger = get_logger("worker")

_TZ = ZoneInfo(TIMEZONE)
scheduler: AsyncIOScheduler | None = None

# APScheduler 默认 misfire_grace_time=1s：主机休眠/事件循环卡住稍久，错过的任务会被
# 直接判为过期（P0-7 整日缺数的直接原因之一）。给日更链路留出当天补跑窗口；
# 跨天缺口由 app.catch_up 的历史口径补跑处理。
_DAY_GRACE_SEC = 3 * 3600
_NIGHT_GRACE_SEC = 6 * 3600


# ============================================================
# 任务体
# ============================================================

async def _with_lock(name: str, job) -> None:
    """带 advisory lock 执行，拿不到锁就跳过（多实例部署安全）。"""
    from app.db.session import AsyncSessionLocal
    from app.pipeline import advisory_unlock, try_advisory_lock

    async with AsyncSessionLocal() as db:
        acquired = False
        try:
            acquired = await try_advisory_lock(db, f"gfna:{name}")
            if not acquired:
                logger.info("任务 %s 已在其他实例运行，跳过", name)
                return
            with bind_run(task=name):
                await job(db)
        finally:
            if acquired:
                await advisory_unlock(db, f"gfna:{name}")


async def run_daily_pipeline_job(db) -> None:
    """盘后串行链路。仅在真实交易日执行（修 #17：不再用 weekday 判断）。"""
    from app.daily_chain import run_daily_chain
    from app.ingest.calendar import is_trade_day

    today = date.today()
    if not await is_trade_day(db, today):
        logger.info("%s 非交易日，跳过盘后链路", today)
        return

    result = await run_daily_chain(db, today, run_data=True, reason="scheduler")
    stages = result.get("stages") or {}
    logger.info(
        "盘后链路结束 status=%s stages=%s failed_stage=%s",
        result.get("status"), list(stages), result.get("failed_stage"),
    )


async def run_daily_chain_watchdog_job(db) -> None:
    """盘后巡检：数据未完成且当日还有重采额度时补采；否则续跑罗盘/推送。"""
    from app.daily_chain import (
        data_readiness,
        resolve_chain_date,
        run_daily_chain,
        should_retry_data,
    )
    from app.pipeline import advisory_unlock, try_advisory_lock

    # 数据流水线仍在跑时不要抢跑，也不要因"数据未就绪"误告警。
    try:
        pipeline_free = await try_advisory_lock(db, "gfna:daily_pipeline")
    except Exception:  # noqa: BLE001 - 巡检失败不影响下一轮
        logger.exception("巡检获取数据流水线锁失败")
        return
    if not pipeline_free:
        logger.info("盘后数据流水线运行中，巡检暂缓")
        return
    try:
        await advisory_unlock(db, "gfna:daily_pipeline")
    except Exception:  # noqa: BLE001
        logger.warning("巡检释放数据流水线锁失败", exc_info=True)

    target = None
    retry_data = False
    try:
        target = await resolve_chain_date(db)
        if target is not None:
            ready, _detail = await data_readiness(db, target)
            if not ready:
                retry_data = await should_retry_data(db, target)
    except Exception:  # noqa: BLE001 - 巡检判断失败时退回普通续跑
        logger.exception("巡检判断数据状态失败，按普通续跑处理")

    result = await run_daily_chain(
        db,
        target,
        run_data=retry_data,
        reason="watchdog_retry" if retry_data else "watchdog",
        notify_on_blocked=not retry_data,
    )
    if result.get("status") not in {"skipped", "blocked"}:
        logger.info(
            "盘后巡检推进 status=%s trade_date=%s stages=%s retry_data=%s",
            result.get("status"), result.get("trade_date"),
            list((result.get("stages") or {}).keys()), retry_data,
        )


async def run_observation_algos(db) -> list[dict]:
    """运行观察候选策略（不进入 SPECS/API/共识），供每日推送查看。"""
    from app.core.config import OBSERVATION_ALGOS
    from app.strategy import runner as stg_runner
    from app.strategy.strategies import CANDIDATE_SPECS

    codes = [code for code in OBSERVATION_ALGOS if code in CANDIDATE_SPECS]
    if not codes:
        return []
    trade_date = await stg_runner.latest_trade_date(db)
    if trade_date is None:
        logger.info("无可用交易日，跳过观察策略")
        return []
    results: list[dict] = []
    for code in codes:
        try:
            result = await stg_runner.run_strategy(
                code, trade_date, {"limit": 3}, force=False, db=db, is_backtest=False
            )
            await db.commit()
            results.append(result)
            logger.info("观察策略完成 algo=%s date=%s signals=%d", code, trade_date, result.get("count", 0))
        except Exception as exc:  # noqa: BLE001 - 单策略失败不影响其余
            await db.rollback()
            logger.error("观察策略失败 algo=%s: %s", code, type(exc).__name__)
    return results


async def run_signal_perf_job(db) -> None:
    """重算最近 25 个交易日的信号前瞻收益（依赖当日选股结果）。"""
    from app.ingest.calendar import latest_trade_date
    from app.pipeline import run_task

    latest = await latest_trade_date(db, on_or_before=date.today())
    if latest is None:
        logger.info("无可用交易日，跳过信号前瞻统计")
        return
    await run_task("signal_perf", trade_date=latest, db=db)


async def run_paper_job(db) -> None:
    """纸面组合增量重算（V6 T11 / P2-3）。非交易日不写行；单组合失败已在引擎内隔离。"""
    from app.engine.paper import sync_all_paper_portfolios
    from app.ingest.calendar import is_trade_day

    today = date.today()
    if not await is_trade_day(db, today):
        logger.info("%s 非交易日，跳过纸面组合", today)
        return

    results = await sync_all_paper_portfolios(db)
    failed = [item["algo"] for item in results if item.get("status") == "failed"]
    logger.info("纸面组合结束 total=%d failed=%s", len(results), failed)


async def run_sector_members_job(db) -> None:
    """每周全量板块成分（目标池；SCD2）。"""
    from app.ingest.calendar import latest_trade_date
    from app.pipeline import run_task

    latest = await latest_trade_date(db, on_or_before=date.today())
    if latest is None:
        logger.info("无可用交易日，跳过板块成分周更")
        return
    await run_task("sector_members", trade_date=latest, db=db)


async def run_moneyflow_job(db) -> None:
    """每周全市场个股资金流（每日只保留板块级资金流）。"""
    from app.ingest.calendar import latest_trade_date
    from app.pipeline import run_task

    latest = await latest_trade_date(db, on_or_before=date.today())
    if latest is None:
        logger.info("无可用交易日，跳过资金流周更")
        return
    await run_task("moneyflow", trade_date=latest, force=True, db=db)


async def run_news_crawl_job(db) -> None:
    """新闻采集（国内四源 JSON 快讯；失败不阻断主链）。"""
    from app.services.news_collector import collect_news

    try:
        await collect_news(db)
    except Exception:                                  # noqa: BLE001
        logger.exception("新闻采集失败（非阻断）")


async def run_backfill_job(db) -> None:
    """夜间补齐历史日线（断点续传，失败可继续）。"""
    from app.pipeline import run_task
    await run_task("backfill", db=db)


async def run_backup_job(db) -> None:
    """每日首次巡检时备份数据库；失败发告警但不影响其它任务。"""
    import asyncio

    from app.services.backup import backup_database, backup_due

    if not backup_due():
        return
    result = await asyncio.to_thread(backup_database, reason="scheduler")
    if result["status"] == "success":
        logger.info(
            "数据库备份完成 file=%s bytes=%s", result.get("path"), result.get("bytes")
        )
        return
    if result["status"] == "skipped":
        logger.info("数据库备份跳过：%s", result.get("reason"))
        return
    logger.error("数据库备份失败：%s", result.get("error"))
    try:
        from app.notify.alerts import send_chain_alert

        await send_chain_alert(
            db, date.today(), "backup",
            f"数据库备份失败：{result.get('error')}",
        )
    except Exception:                                  # noqa: BLE001
        logger.exception("数据库备份告警发送失败")


async def run_compass_paper_backfill_job(db) -> None:
    """17:40 把纸面组合净值聚合回填到罗盘复盘表（依赖 17:30 纸面组合同步）。"""
    from app.compass.paper_backfill import backfill_paper_portfolio
    from app.ingest.calendar import is_trade_day

    today = date.today()
    if not await is_trade_day(db, today):
        logger.info("%s 非交易日，跳过罗盘复盘回填", today)
        return
    try:
        result = await backfill_paper_portfolio(db)
        logger.info("罗盘复盘回填结束 rows=%s portfolios=%s status=%s",
                    result.get("rows"), result.get("portfolios"), result.get("status"))
    except Exception as exc:  # noqa: BLE001 - 回填失败不影响其他任务
        logger.error("罗盘复盘回填失败: %s", type(exc).__name__)


# ============================================================
# 调度器
# ============================================================

def start_scheduler() -> AsyncIOScheduler:
    """注册并启动定时任务。"""
    global scheduler
    if scheduler is not None:
        return scheduler

    scheduler = AsyncIOScheduler(timezone=_TZ)
    # 15:36 而不是 15:05：config.SNAPSHOT_READY_* 默认 15:30，采集层会拒绝"未收盘就绪"的数据，
    # 早于该时刻跑会让 snapshot/sector/moneyflow 全部被跳过（见 ingest/base 的 before_close_ready_time）
    scheduler.add_job(_with_lock, CronTrigger(hour=15, minute=36, timezone=_TZ),
                      args=["daily_pipeline", run_daily_pipeline_job],
                      id="daily_pipeline", name="盘后串行链路", replace_existing=True,
                      misfire_grace_time=_DAY_GRACE_SEC)
    # 盘后巡检：固定时点只作机会触发；进程晚启动/中途失败时由巡检续跑罗盘与推送
    scheduler.add_job(_with_lock, CronTrigger(hour="15-23", minute="5,35", timezone=_TZ),
                      args=["daily_chain_watchdog", run_daily_chain_watchdog_job],
                      id="daily_chain_watchdog", name="盘后链路巡检",
                      replace_existing=True, misfire_grace_time=_NIGHT_GRACE_SEC)
    # 每天 16:10 起每 2 小时检查一次；当天已有成功备份则空转，失败会在下个时点重试
    scheduler.add_job(_with_lock, CronTrigger(hour="16-23", minute="10", timezone=_TZ),
                      args=["backup", run_backup_job],
                      id="backup", name="数据库备份", replace_existing=True,
                      misfire_grace_time=_NIGHT_GRACE_SEC)
    scheduler.add_job(_with_lock, CronTrigger(hour=17, minute=10, timezone=_TZ),
                      args=["signal_perf", run_signal_perf_job],
                      id="signal_perf", name="信号前瞻统计", replace_existing=True,
                      misfire_grace_time=_DAY_GRACE_SEC)
    scheduler.add_job(_with_lock, CronTrigger(hour=17, minute=30, timezone=_TZ),
                      args=["paper", run_paper_job],
                      id="paper", name="纸面组合跟踪", replace_existing=True,
                      misfire_grace_time=_DAY_GRACE_SEC)
    scheduler.add_job(_with_lock, CronTrigger(hour=17, minute=40, timezone=_TZ),
                      args=["compass_paper", run_compass_paper_backfill_job],
                      id="compass_paper", name="罗盘复盘回填", replace_existing=True,
                      misfire_grace_time=_DAY_GRACE_SEC)
    if SECTOR_MEMBERS_WEEKLY:
        scheduler.add_job(_with_lock, CronTrigger(day_of_week="sun", hour=11, minute=30, timezone=_TZ),
                          args=["sector_members_weekly", run_sector_members_job],
                          id="sector_members_weekly", name="板块成分周更",
                          replace_existing=True, misfire_grace_time=_NIGHT_GRACE_SEC)
    if MONEYFLOW_FULL_WEEKLY:
        scheduler.add_job(_with_lock, CronTrigger(day_of_week="sun", hour=12, minute=0, timezone=_TZ),
                          args=["moneyflow_weekly", run_moneyflow_job],
                          id="moneyflow_weekly", name="个股资金流周更",
                          replace_existing=True, misfire_grace_time=_NIGHT_GRACE_SEC)
    scheduler.add_job(_with_lock, CronTrigger(hour=2, minute=0, timezone=_TZ),
                      args=["backfill", run_backfill_job],
                      id="backfill", name="历史补齐", replace_existing=True,
                      misfire_grace_time=_NIGHT_GRACE_SEC)
    scheduler.add_job(_with_lock, "interval", minutes=CRAWL_INTERVAL_MINUTES,
                      args=["news", run_news_crawl_job],
                      id="news_crawl", name="新闻采集", replace_existing=True)

    scheduler.start()
    logger.info("定时任务已启动：15:36 盘后串行链路（数据→罗盘→推送）"
                " / 盘后每 30 分钟巡检 / 16:10 起每 2 小时数据库备份"
                " / 17:10 信号统计 / 17:30 纸面"
                " / 17:40 罗盘复盘回填 / 周日 11:30 板块成分 / 周日 12:00 资金流"
                " / 02:00 补齐 / 每 %d 分钟新闻", CRAWL_INTERVAL_MINUTES)
    return scheduler


def stop_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
            logger.info("定时任务已停止")
        except Exception:                              # noqa: BLE001
            pass
        scheduler = None


def job_status() -> list[dict]:
    if scheduler is None:
        return []
    return [
        {"id": job.id, "name": job.name,
         "next_run": job.next_run_time.isoformat() if job.next_run_time else None}
        for job in scheduler.get_jobs()
    ]


__all__ = ["start_scheduler", "stop_scheduler", "job_status",
           "run_daily_pipeline_job", "run_daily_chain_watchdog_job",
           "run_observation_algos", "run_signal_perf_job",
           "run_paper_job", "run_news_crawl_job", "run_backfill_job",
           "run_sector_members_job", "run_moneyflow_job",
           "run_backup_job", "run_compass_paper_backfill_job"]
