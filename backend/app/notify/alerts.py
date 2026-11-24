"""盘后串行链路异常告警（企业微信）。

- 复用 `WECOM_WEBHOOK_URL`，不额外引入通知通道；
- 幂等：`push_log(run_id="chain-alert:<date>:<stage>", channel="chain_alert")`，
  同一交易日同一环节只成功发送一次，失败可被巡检重试；
- 告警失败只记日志，绝不影响主链路返回。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import PUSH_DRY_RUN, WECOM_WEBHOOK_URL
from app.core.logging import get_logger
from app.db.models import PushLog
from app.notify.push import post_wecom_markdown

logger = get_logger("chain_alert")

ALERT_CHANNEL = "chain_alert"
STAGE_LABELS = {
    "data": "数据采集与入库校验",
    "compass": "仓位罗盘分析",
    "push": "机器人推送",
    "backup": "数据库备份",
}


def build_chain_alert_message(
    trade_date: date,
    stage: str,
    error: str,
    *,
    blocked: list[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """构造告警 markdown；只陈述任务状态，不含投资建议。"""
    label = STAGE_LABELS.get(stage, stage)
    lines = [
        "## GFNA 盘后链路异常",
        "",
        f"**交易日**：{trade_date.isoformat()}",
        f"**失败环节**：{label}（{stage}）",
        f"**错误**：{error}",
    ]
    if blocked:
        lines.append(f"**已阻止**：{'、'.join(blocked)}")
    detail = detail or {}
    missing_datasets = detail.get("missing_datasets") or []
    if missing_datasets:
        names = [
            f"{item.get('dataset')}({item.get('step')})"
            if isinstance(item, dict) else str(item)
            for item in missing_datasets[:8]
        ]
        lines.append(f"**缺失数据集**：{', '.join(names)}")
    missing_algos = detail.get("missing_algos") or []
    if missing_algos:
        lines.append(f"**缺失选股结果**：{', '.join(map(str, missing_algos[:8]))}")
    for key, label in (("failed_steps", "失败步骤"), ("partial_steps", "部分失败步骤")):
        steps = detail.get(key) or {}
        if isinstance(steps, dict) and steps:
            rendered = [
                f"{name}:{str(reason)[:60]}"
                for name, reason in list(steps.items())[:6]
            ]
            lines.append(f"**{label}**：{'; '.join(rendered)}")
    lines.extend([
        "",
        "> 本消息为任务运行告警，不构成投资建议。",
    ])
    return "\n".join(lines)[:3900]


async def send_chain_alert(
    db: AsyncSession,
    trade_date: date,
    stage: str,
    error: str,
    *,
    blocked: list[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发送链路失败告警；同一日同一环节已发送则跳过。永不抛异常。"""
    run_id = f"chain-alert:{trade_date.isoformat()}:{stage}"
    try:
        existing = (await db.execute(
            select(PushLog).where(
                PushLog.run_id == run_id,
                PushLog.channel == ALERT_CHANNEL,
            )
        )).scalar_one_or_none()
        if existing is not None and existing.status == "sent":
            return {"status": "skipped", "reason": "already_sent", "run_id": run_id}

        if existing is None:
            existing = PushLog(
                trade_date=trade_date,
                algo_code="chain",
                run_id=run_id,
                channel=ALERT_CHANNEL,
                status="pending",
                attempts=0,
            )
            db.add(existing)
        else:
            existing.status = "pending"
            existing.error = None

        message = build_chain_alert_message(
            trade_date, stage, error, blocked=blocked, detail=detail
        )

        if PUSH_DRY_RUN or not WECOM_WEBHOOK_URL:
            existing.status = "dry_run" if PUSH_DRY_RUN else "skipped"
            existing.error = None if PUSH_DRY_RUN else "WECOM_WEBHOOK_URL 未配置"
            await db.commit()
            return {"status": existing.status, "run_id": run_id}

        try:
            attempts = await post_wecom_markdown(WECOM_WEBHOOK_URL, message)
        except Exception as exc:  # noqa: BLE001 - 告警失败不能影响主流程
            existing.status = "failed"
            existing.attempts = max(existing.attempts or 0, 3)
            existing.error = f"{type(exc).__name__}: {exc}"[:2000]
            await db.commit()
            logger.error("链路告警发送失败 stage=%s: %s", stage, existing.error)
            return {"status": "failed", "run_id": run_id, "error": existing.error}

        existing.status = "sent"
        existing.attempts = attempts
        existing.error = None
        existing.sent_at = datetime.now(timezone.utc)
        await db.commit()
        logger.warning("链路告警已发送 trade_date=%s stage=%s", trade_date, stage)
        return {"status": "sent", "run_id": run_id, "attempts": attempts}
    except Exception as exc:  # noqa: BLE001 - 兜底：告警链路自身故障
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.exception("链路告警异常 stage=%s: %s", stage, type(exc).__name__)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


__all__ = ["ALERT_CHANNEL", "STAGE_LABELS", "build_chain_alert_message", "send_chain_alert"]
