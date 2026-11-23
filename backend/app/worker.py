"""
定时任务（方案 A8 / F2）
========================
单进程 APScheduler + 数据库 advisory lock：
  - 15:36 盘后流水线（真实交易日历判断，节假日不跑）
  - 16:30 选股（等评分与日报就绪）
  - 17:10 信号前瞻统计（依赖 16:30 选股结果）
  - 17:30 纸面组合（水位 + 60 日重叠窗口，每周全量校准；非交易日跳过）
  - 02:00 历史补齐（断点续传，夜间执行）
  - 每 N 分钟新闻采集（作为情绪因子输入）

**为什么不用 Celery**：方案 F2 明确"先单进程 + DB 锁，量大再上队列"。
`pg_try_advisory_lock` 保证多 worker 部署时同一时刻只有一个实例在跑，定时任务因此幂等。
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
    """盘后全链路。仅在真实交易日执行（修 #17：不再用 weekday 判断）。"""
    from app.ingest.calendar import is_trade_day
    from app.pipeline import run_daily_pipeline

    today = date.today()
    if not await is_trade_day(db, today):
        logger.info("%s 非交易日，跳过盘后流水线", today)
        return

    result = await run_daily_pipeline(today, db)
    failed = [k for k, v in result["steps"].items()
              if isinstance(v, dict) and v.get("status") == "failed"]
    logger.info("盘后流水线结束 steps=%d failed=%s", len(result["steps"]), failed)


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


async def run_algo_job(db) -> None:
    """跑当前启用策略（结果先落库，前端打开即热），成功后可选推送。"""
    from app.pipeline import run_task

    await run_task("algos", db=db)
    await run_observation_algos(db)
    try:
        from app.notify.push import push_daily_results

        result = await push_daily_results(db)
        if result.get("error"):
            logger.error("盘后推送未完成: %s", result["error"])
    except Exception as exc:  # noqa: BLE001 - 推送失败不得影响选股任务
        logger.error("盘后推送异常（已隔离）: %s", type(exc).__name__)


async def run_signal_perf_job(db) -> None:
    """重算最近 25 个交易日的信号前瞻收益（依赖 16:30 选股结果）。"""
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


async def run_etf_sync_job(db) -> None:
    """16:10 同步 ETF 日行情与板块映射，供 16:20 决策卡使用。"""
    from app.compass.etf_sync import sync_etf_data
    from app.ingest.calendar import is_trade_day

    today = date.today()
    if not await is_trade_day(db, today):
        logger.info("%s 非交易日，跳过 ETF 同步", today)
        return
    try:
        result = await sync_etf_data(db, today, mode="auto")
        logger.info("ETF 同步结束 status=%s mapped=%s coverage=%s",
                    result.get("status"), result.get("mapped"), result.get("coverage"))
    except Exception as exc:  # noqa: BLE001 - ETF 同步失败不阻断其他任务
        logger.error("ETF 同步失败: %s", type(exc).__name__)


async def run_compass_card_job(db) -> None:
    """16:20 生成仓位罗盘决策卡。数据未就绪时记录错误，不生成半成品。"""
    from app.compass.service import generate_card
    from app.ingest.calendar import is_trade_day

    today = date.today()
    if not await is_trade_day(db, today):
        logger.info("%s 非交易日，跳过仓位罗盘卡片", today)
        return
    try:
        card = await generate_card(db, today)
        logger.info("仓位罗盘卡片完成 date=%s card_id=%s exec_cap=%s",
                    today, card.card_id, card.exec_cap)
    except Exception as exc:  # noqa: BLE001 - 数据未就绪不阻断其他任务
        logger.error("仓位罗盘卡片生成失败 date=%s: %s", today, type(exc).__name__)


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


async def run_compass_push_job(db) -> None:
    """17:00 推送当日仓位罗盘卡片；无卡片或已推送时跳过。"""
    from sqlalchemy import select

    from app.compass.push import send_card_push
    from app.db.models import FactDecisionCard
    from app.ingest.calendar import is_trade_day

    today = date.today()
    if not await is_trade_day(db, today):
        logger.info("%s 非交易日，跳过仓位罗盘推送", today)
        return
    card = (await db.execute(
        select(FactDecisionCard)
        .where(FactDecisionCard.trade_date == today)
        .order_by(FactDecisionCard.generated_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if card is None:
        logger.warning("仓位罗盘推送跳过：%s 无决策卡", today)
        return
    try:
        result = await send_card_push(db, card.card_id)
        logger.info("仓位罗盘推送结束 card_id=%s status=%s", card.card_id, result.get("status"))
    except Exception as exc:  # noqa: BLE001 - 推送失败不影响页面
        logger.error("仓位罗盘推送失败 card_id=%s: %s", card.card_id, type(exc).__name__)


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
                      id="daily_pipeline", name="盘后流水线", replace_existing=True,
                      misfire_grace_time=_DAY_GRACE_SEC)
    scheduler.add_job(_with_lock, CronTrigger(hour=16, minute=30, timezone=_TZ),
                      args=["algos", run_algo_job],
                      id="algos", name="策略选股", replace_existing=True,
                      misfire_grace_time=_DAY_GRACE_SEC)
    scheduler.add_job(_with_lock, CronTrigger(hour=16, minute=10, timezone=_TZ),
                      args=["etf_sync", run_etf_sync_job],
                      id="etf_sync", name="ETF 同步与映射", replace_existing=True,
                      misfire_grace_time=_DAY_GRACE_SEC)
    scheduler.add_job(_with_lock, CronTrigger(hour=16, minute=20, timezone=_TZ),
                      args=["compass_card", run_compass_card_job],
                      id="compass_card", name="仓位罗盘卡片", replace_existing=True,
                      misfire_grace_time=_DAY_GRACE_SEC)
    scheduler.add_job(_with_lock, CronTrigger(hour=17, minute=0, timezone=_TZ),
                      args=["compass_push", run_compass_push_job],
                      id="compass_push", name="仓位罗盘推送", replace_existing=True,
                      misfire_grace_time=_DAY_GRACE_SEC)
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
    logger.info("定时任务已启动：15:36 盘后 / 16:10 ETF 同步 / 16:20 仓位罗盘"
                " / 16:30 选股 / 17:00 罗盘推送 / 17:10 信号统计 / 17:30 纸面"
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
           "run_daily_pipeline_job", "run_algo_job", "run_signal_perf_job",
           "run_paper_job", "run_news_crawl_job", "run_backfill_job",
           "run_sector_members_job", "run_moneyflow_job",
           "run_compass_card_job", "run_compass_push_job",
           "run_compass_paper_backfill_job", "run_etf_sync_job"]
