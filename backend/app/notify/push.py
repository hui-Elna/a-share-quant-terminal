"""企业微信群机器人盘后推送（V8 W3）。

- 只推送当前 SPECS 启用策略、status=success、is_backtest=false 的最新 run；
- `(run_id, channel)` 唯一，已 sent 的不重复推送；
- 发送失败只记日志，不影响选股任务；
- Webhook URL 只从环境变量读取，日志不输出完整 URL。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    PUSH_ALGOS,
    PUSH_CHANNEL,
    PUSH_DRY_RUN,
    PUSH_ENABLED,
    PUSH_FRONTEND_URL,
    PUSH_MODE,
    PUSH_OBSERVATION,
    PUSH_TOP_N,
    WECOM_WEBHOOK_URL,
)
from app.db.models.push import PushLog
from app.db.models.strategy import StgRun, StgSignal
from app.strategy.strategies import CANDIDATE_SPECS, SPECS, get_spec

logger = logging.getLogger(__name__)

_MAX_BYTES = 4096


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _summary_for(algo_code: str, score: Any, metrics: dict[str, Any]) -> str:
    if algo_code == "a3":
        value = _num(metrics.get("composite_score")) or _num(score)
        return f"评分 {value:.2f}" if value is not None else "评分 —"
    if algo_code == "a4":
        dividend = _num(metrics.get("dv_ttm"))
        pe = _num(metrics.get("pe_ttm"))
        parts = []
        if dividend is not None:
            parts.append(f"股息 {dividend:.2f}%")
        if pe is not None:
            parts.append(f"PE {pe:.1f}")
        return " / ".join(parts) or f"得分 {_num(score) or 0:.2f}"
    if algo_code == "c1":
        cap = _num(metrics.get("float_cap"))
        pct = _num(metrics.get("pct_chg"))
        parts = []
        if cap is not None:
            parts.append(f"流通市值 {cap / 1e8:.0f}亿")
        if pct is not None:
            parts.append(f"涨 {pct:.2f}%")
        return " / ".join(parts) or "突破"
    if algo_code == "c2":
        ratio = _num(metrics.get("range40_ratio"))
        return f"区间比 {ratio:.2f}" if ratio is not None else "高窄旗形"
    if algo_code == "c3":
        rps = _num(metrics.get("rps"))
        return f"RPS {rps:.1f}" if rps is not None else "RPS 突破"
    if algo_code == "c6":
        band = _num(metrics.get("boll_width_pct60"))
        return f"带宽分位 {band:.1f}%" if band is not None else "布林挤压"
    value = _num(score)
    return f"得分 {value:.2f}" if value is not None else "—"


def _truncate_utf8(text: str, limit: int = _MAX_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[: max(0, limit - 24)].decode("utf-8", errors="ignore") + "\n…（已截断）"


def _fmt_reason_value(value: Any) -> str:
    number = _num(value)
    if number is not None:
        return f"{number:g}"
    return str(value)


def _one_line_reason(item: dict[str, Any]) -> str:
    """从 metrics.triggered / score_breakdown 复述条件，缺失则“归因缺失”。"""
    metrics = item.get("metrics") or {}
    parts: list[str] = []
    triggered = metrics.get("triggered")
    if isinstance(triggered, list):
        for condition in triggered:
            if not isinstance(condition, dict) or not condition.get("passed"):
                continue
            label = str(condition.get("label") or condition.get("key") or "").strip()
            if not label:
                continue
            actual = condition.get("actual")
            op = condition.get("op")
            threshold = condition.get("threshold")
            if actual is not None and op and threshold is not None:
                text = f"{label}（{_fmt_reason_value(actual)} {op} {_fmt_reason_value(threshold)}）"
            elif actual is not None:
                text = f"{label}（{_fmt_reason_value(actual)}）"
            else:
                text = label
            parts.append(text)
            if len(parts) >= 2:
                break
    if not parts:
        breakdown = metrics.get("score_breakdown")
        if isinstance(breakdown, dict):
            for part in (breakdown.get("parts") or [])[:2]:
                if not isinstance(part, dict):
                    continue
                name = str(part.get("name") or "").strip()
                score = _num(part.get("score"))
                if name and score is not None:
                    parts.append(f"{name} {score:.1f}")
    return ("；".join(parts) or "归因缺失")[:50]


def build_message(
    trade_date: date,
    sections: list[dict[str, Any]],
    frontend_url: str = PUSH_FRONTEND_URL,
) -> str:
    lines = [f"## GFNA 选股播报 | {trade_date.isoformat()}", ""]
    for section in sections:
        name = section.get("algo_name") or section["algo_code"]
        count = int(section.get("signal_count") or 0)
        suffix = "（观察·未通过回测）" if section.get("observed") else ""
        lines.append(f"**{name}（{section['algo_code']}）{suffix}**（{count} 只）")
        items = section.get("items") or []
        if not items:
            lines.append("无信号")
        else:
            for item in items:
                reason = _one_line_reason(item)
                lines.append(
                    f"{item['rank']}. {item['name']}({item['code']}) "
                    f"{item['summary']} — {reason}"
                )
        lines.append("")
    lines.append(f"查看：{frontend_url}")
    footer = "\n\n条件复述，非投资建议"
    body = _truncate_utf8(
        "\n".join(lines), _MAX_BYTES - len(footer.encode("utf-8")) - 32
    )
    return body + footer


class WeComResponseError(RuntimeError):
    """企业微信返回非 0 errcode；只保留脱敏后的状态码与错误文案。"""

    def __init__(self, status_code: int, errcode: Any, errmsg: str) -> None:
        self.status_code = status_code
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"status={status_code} errcode={errcode} errmsg={errmsg}")


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, WeComResponseError):
        return f"WeComResponseError: {exc}"
    return f"{type(exc).__name__}: 企业微信推送请求失败"


async def post_wecom_markdown(url: str, content: str, *, max_attempts: int = 3) -> int:
    """发送 markdown；成功返回尝试次数，失败抛最后一个异常。"""
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            # 本机 NO_PROXY 含 IPv6/网段条目，httpx 0.27.2 在 trust_env=True 下
            # 会把这些条目误当端口解析；企业微信走直连，不读取环境代理。
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                response = await client.post(url, json=payload)
            body = response.json()
            errcode = body.get("errcode")
            if response.status_code != 200 or errcode != 0:
                errmsg = str(body.get("errmsg") or "")[:200]
                raise WeComResponseError(response.status_code, errcode, errmsg)
            return attempt
        except Exception as exc:  # noqa: BLE001 - 需要覆盖网络与返回体异常
            last_exc = exc
            if attempt < max_attempts:
                await asyncio.sleep(2 ** (attempt - 1))
    raise last_exc or RuntimeError("企业微信推送失败")


def _signal_item(algo_code: str, row: StgSignal) -> dict[str, Any]:
    metrics = row.metrics or {}
    return {
        "code": row.code,
        "name": row.name or row.code,
        "rank": int(row.rank or 0),
        "score": _num(row.score),
        "summary": _summary_for(algo_code, row.score, metrics),
        "metrics": metrics,
    }


async def _load_sections(
    db: AsyncSession,
    trade_date: date,
    algos: list[str],
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for code in algos:
        spec = get_spec(code)
        observed = code in CANDIDATE_SPECS
        result = await db.execute(
            select(StgRun)
            .where(StgRun.algo_code == code)
            .where(StgRun.trade_date == trade_date)
            .where(StgRun.status == "success")
            .where(StgRun.is_backtest.is_(False))
            .order_by(StgRun.created_at.desc())
            .limit(1)
        )
        run = result.scalar_one_or_none()
        items: list[dict[str, Any]] = []
        if run is not None:
            rows = (
                await db.execute(
                    select(StgSignal)
                    .where(StgSignal.run_id == run.run_id)
                    .order_by(StgSignal.rank.asc())
                    .limit(max(1, int(PUSH_TOP_N)))
                )
            ).scalars().all()
            items = [_signal_item(code, row) for row in rows]
        sections.append(
            {
                "algo_code": code,
                "algo_name": spec.name,
                "run_id": run.run_id if run is not None else None,
                "signal_count": int(getattr(run, "signal_count", 0) or 0),
                "items": items,
                "observed": observed,
            }
        )
    return sections


async def _latest_trade_date(db: AsyncSession, algos: list[str]) -> date | None:
    result = await db.execute(
        select(func.max(StgRun.trade_date))
        .where(StgRun.algo_code.in_(algos))
        .where(StgRun.status == "success")
        .where(StgRun.is_backtest.is_(False))
    )
    return result.scalar()


async def _existing_push(db: AsyncSession, run_id: str, channel: str) -> PushLog | None:
    result = await db.execute(
        select(PushLog).where(PushLog.run_id == run_id, PushLog.channel == channel)
    )
    return result.scalar_one_or_none()


async def push_daily_results(
    db: AsyncSession,
    trade_date: date | None = None,
    *,
    dry_run: bool | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """推送指定交易日（默认最近一次成功选股日）的结果。永不抛异常。"""
    channel = (channel or PUSH_CHANNEL).lower()
    dry = PUSH_DRY_RUN if dry_run is None else dry_run
    if not PUSH_ENABLED and not dry:
        return {"enabled": False, "sent": 0}
    if channel != "wecom":
        return {"error": f"不支持的推送通道: {channel}"}

    allowed = dict(SPECS)
    if PUSH_OBSERVATION:
        allowed.update(CANDIDATE_SPECS)
    algos = [code for code in (PUSH_ALGOS or tuple(allowed)) if code in allowed]
    try:
        if trade_date is None:
            trade_date = await _latest_trade_date(db, algos)
        if trade_date is None:
            return {"error": "没有可推送的选股 run"}
        sections = await _load_sections(db, trade_date, algos)
        if PUSH_MODE == "signals_only":
            sections = [item for item in sections if item["signal_count"] > 0]
        if not sections:
            return {"skipped": "没有满足推送模式的策略", "trade_date": trade_date.isoformat()}

        pending_sections: list[dict[str, Any]] = []
        rows: list[PushLog] = []
        for section in sections:
            run_id = section["run_id"]
            if not run_id:
                pending_sections.append(section)
                continue
            existing = await _existing_push(db, run_id, channel)
            if existing is not None and existing.status == "sent":
                continue
            if existing is None:
                existing = PushLog(
                    trade_date=trade_date,
                    algo_code=section["algo_code"],
                    run_id=run_id,
                    channel=channel,
                    status="pending",
                    attempts=0,
                )
                db.add(existing)
            else:
                existing.status = "pending"
                existing.error = None
            rows.append(existing)
            pending_sections.append(section)
        if not pending_sections:
            return {"skipped": "全部已推送", "trade_date": trade_date.isoformat()}

        message = build_message(trade_date, pending_sections)
        if dry:
            await db.rollback()
            return {
                "dry_run": True,
                "trade_date": trade_date.isoformat(),
                "message": message,
                "sections": pending_sections,
            }

        if not WECOM_WEBHOOK_URL:
            await db.rollback()
            return {"error": "WECOM_WEBHOOK_URL 未配置"}

        await db.flush()
        try:
            attempts = await post_wecom_markdown(WECOM_WEBHOOK_URL, message)
        except Exception as exc:  # noqa: BLE001
            error = _safe_error(exc)
            for row in rows:
                row.status = "failed"
                row.attempts = max(row.attempts or 0, 3)
                row.error = error
            await db.commit()
            logger.error("企业微信推送失败: %s", error)
            return {
                "sent": 0,
                "failed": len(rows),
                "trade_date": trade_date.isoformat(),
                "error": error,
            }

        sent_at = datetime.now(timezone.utc)
        for row in rows:
            row.status = "sent"
            row.attempts = attempts
            row.error = None
            row.sent_at = sent_at
        await db.commit()
        logger.info("企业微信推送成功 trade_date=%s sections=%d", trade_date, len(rows))
        return {
            "sent": len(rows),
            "trade_date": trade_date.isoformat(),
            "bytes": len(message.encode("utf-8")),
        }
    except Exception as exc:  # noqa: BLE001 - 推送失败不得影响主任务
        await db.rollback()
        error = _safe_error(exc)
        logger.error("盘后推送异常（已隔离）: %s", error)
        return {"error": error}
