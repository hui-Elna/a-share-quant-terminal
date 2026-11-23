"""板块目标池解析（V10 数据采集瘦身）。

目标：把“全市场板块”收敛为“可交易的目标池”，供板块快照、板块历史、
板块成分采集统一使用。本模块只读现有表，不新增数据库表。

口径：
- 行业板块默认全部进入候选池；
- 主题板块只保留 ``SECTOR_THEME_WHITELIST`` 中显式配置的代码/名称；
- 可交易性：ETF 映射、ETF 20 日均成交额、ETF 规模、板块有效成分数；
- 缺失值保留 ``None``，不补 0；ETF 规模缺失时只告警、不因缺失直接淘汰。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import config
from app.core.logging import get_logger
from app.core.status import QualityStatus
from app.db.models.compass import DimEtfSectorMap, FactEtfDaily
from app.db.models.dim import DimSectorMember
from app.services.freshness import write_freshness

logger = get_logger(__name__)

#: 同一交易日/同一配置的解析结果缓存；进程内复用，避免每个任务重复查库。
_CACHE: dict[str, "SectorPool"] = {}


@dataclass(frozen=True)
class SectorPool:
    """一次板块池解析结果。``actionable_codes`` 才是每日采集目标。"""

    trade_date: date
    industry_codes: tuple[str, ...]
    theme_codes: tuple[str, ...]
    actionable_codes: tuple[str, ...]
    excluded: dict[str, str]
    version: str


def _normalize(value: Any) -> str:
    return str(value or "").strip()


def _config_signature() -> str:
    payload = {
        "mode": config.SECTOR_UNIVERSE_MODE,
        "whitelist": sorted(config.SECTOR_THEME_WHITELIST),
        "min_members": config.SECTOR_MIN_MEMBERS,
        "etf_min_amount": config.ETF_MIN_AMOUNT,
        "etf_min_aum": config.ETF_MIN_AUM,
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def _cache_key(trade_date: date) -> str:
    return f"{trade_date.isoformat()}:{_config_signature()}"


def clear_sector_pool_cache() -> None:
    """测试/运维强制刷新用。"""
    _CACHE.clear()


def _matches_whitelist(board: dict[str, Any], whitelist: Sequence[str]) -> bool:
    if not whitelist:
        return False
    candidates = {
        _normalize(board.get("sector_code")),
        _normalize(board.get("ths_code")),
        _normalize(board.get("sector_name")),
    }
    return any(item in candidates for item in whitelist if item)


def _split_boards(
    boards: Iterable[dict[str, Any]],
    whitelist: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    industry: list[dict[str, Any]] = []
    themes: list[dict[str, Any]] = []
    for board in boards:
        code = _normalize(board.get("sector_code"))
        if not code:
            continue
        if _normalize(board.get("sector_type")) == "industry":
            industry.append(board)
        elif _matches_whitelist(board, whitelist):
            themes.append(board)
    return industry, themes


async def _member_counts(
    db: AsyncSession,
    codes: Sequence[str],
    trade_date: date,
) -> dict[str, int]:
    if not codes:
        return {}
    rows = (await db.execute(
        select(DimSectorMember.sector_code, func.count(func.distinct(DimSectorMember.stock_code)))
        .where(DimSectorMember.sector_code.in_(list(codes)))
        .where(DimSectorMember.effective_from <= trade_date)
        .where(or_(
            DimSectorMember.effective_to.is_(None),
            DimSectorMember.effective_to > trade_date,
        ))
        .where(DimSectorMember.is_partial.is_(False))
        .group_by(DimSectorMember.sector_code)
    )).all()
    return {str(code): int(count or 0) for code, count in rows}


async def _etf_metrics(
    db: AsyncSession,
    sector_codes: Sequence[str],
    trade_date: date,
) -> dict[str, dict[str, Any]]:
    """返回 ``{sector_code: {etf_code, avg_amount, aum}}``。

    只取主映射（``is_primary=True``）且当日有效的 ETF；20 日均额按最近 20 个
    有行情的交易日计算；规模取最近一条 ``aum``。缺失保留 ``None``。
    """
    if not sector_codes:
        return {}
    mappings = (await db.execute(
        select(
            DimEtfSectorMap.sector_code,
            DimEtfSectorMap.etf_code,
            DimEtfSectorMap.etf_name,
        )
        .where(DimEtfSectorMap.sector_code.in_(list(sector_codes)))
        .where(DimEtfSectorMap.is_primary.is_(True))
        .where(DimEtfSectorMap.valid_from <= trade_date)
        .where(or_(
            DimEtfSectorMap.valid_to.is_(None),
            DimEtfSectorMap.valid_to > trade_date,
        ))
    )).all()
    if not mappings:
        return {}

    by_sector: dict[str, dict[str, Any]] = {}
    etf_codes: set[str] = set()
    for sector_code, etf_code, etf_name in mappings:
        by_sector[str(sector_code)] = {
            "etf_code": str(etf_code),
            "etf_name": str(etf_name or etf_code),
        }
        etf_codes.add(str(etf_code))

    rows = (await db.execute(
        select(
            FactEtfDaily.etf_code,
            FactEtfDaily.trade_date,
            FactEtfDaily.amount,
            FactEtfDaily.aum,
        )
        .where(FactEtfDaily.etf_code.in_(sorted(etf_codes)))
        .where(FactEtfDaily.trade_date <= trade_date)
        .order_by(FactEtfDaily.etf_code, FactEtfDaily.trade_date.desc())
    )).all()

    history: dict[str, list[Any]] = {}
    latest_aum: dict[str, Any] = {}
    for etf_code, _day, amount, aum in rows:
        code = str(etf_code)
        history.setdefault(code, [])
        if len(history[code]) < 20 and amount is not None:
            history[code].append(float(amount))
        if code not in latest_aum and aum is not None:
            latest_aum[code] = float(aum)

    for sector_code, item in by_sector.items():
        values = history.get(item["etf_code"]) or []
        item["avg_amount"] = sum(values) / len(values) if values else None
        item["aum"] = latest_aum.get(item["etf_code"])
    return by_sector


async def _resolve_with_session(
    session: AsyncSession,
    trade_date: date,
    *,
    boards: Iterable[dict[str, Any]] | None,
    key: str,
) -> SectorPool:
    if boards is None:
        from app.ingest.ths_board import load_board_list

        boards = await asyncio.to_thread(load_board_list)

    whitelist = sorted({_normalize(item) for item in config.SECTOR_THEME_WHITELIST if _normalize(item)})
    industry_boards, theme_boards = _split_boards(boards, whitelist)
    if config.SECTOR_UNIVERSE_MODE == "industry":
        theme_boards = []

    candidates = industry_boards + theme_boards
    candidate_codes = [_normalize(item.get("sector_code")) for item in candidates]
    member_counts = await _member_counts(session, candidate_codes, trade_date)
    etf_metrics = await _etf_metrics(session, candidate_codes, trade_date)

    actionable: list[str] = []
    excluded: dict[str, str] = {}
    for board in candidates:
        code = _normalize(board.get("sector_code"))
        if not code:
            continue
        members = member_counts.get(code, 0)
        if members < config.SECTOR_MIN_MEMBERS:
            excluded[code] = "insufficient_members"
            continue
        etf = etf_metrics.get(code)
        if etf is None:
            excluded[code] = "no_etf"
            continue
        avg_amount = etf.get("avg_amount")
        if avg_amount is None:
            excluded[code] = "etf_no_data"
            continue
        if avg_amount < config.ETF_MIN_AMOUNT:
            excluded[code] = "etf_illiquid"
            continue
        aum = etf.get("aum")
        if aum is not None and aum < config.ETF_MIN_AUM:
            excluded[code] = "etf_small"
            continue
        actionable.append(code)

    actionable.sort()
    industry_codes = tuple(sorted({
        _normalize(item.get("sector_code")) for item in industry_boards
        if _normalize(item.get("sector_code"))
    }))
    theme_codes = tuple(sorted({
        _normalize(item.get("sector_code")) for item in theme_boards
        if _normalize(item.get("sector_code"))
    }))
    payload = {
        "actionable": actionable,
        "excluded": excluded,
        "mode": config.SECTOR_UNIVERSE_MODE,
        "whitelist": whitelist,
    }
    version = hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    pool = SectorPool(
        trade_date=trade_date,
        industry_codes=industry_codes,
        theme_codes=theme_codes,
        actionable_codes=tuple(actionable),
        excluded=excluded,
        version=version,
    )
    _CACHE[key] = pool
    logger.info(
        "板块池解析 date=%s mode=%s industry=%d theme=%d actionable=%d excluded=%d version=%s",
        trade_date, config.SECTOR_UNIVERSE_MODE, len(industry_codes), len(theme_codes),
        len(actionable), len(excluded), version,
    )
    try:
        await write_freshness(
            session,
            "sector_pool",
            status=QualityStatus.OK,
            mode="success",
            trade_date=trade_date,
            rows=len(actionable),
            coverage=(
                round(len(actionable) / len(candidates), 4) if candidates else 0.0
            ),
            detail={
                "industry": len(industry_codes),
                "theme": len(theme_codes),
                "actionable": len(actionable),
                "excluded": dict(sorted(excluded.items())),
                "version": version,
            },
        )
    except Exception as exc:  # noqa: BLE001 —— 监控写入失败不影响解析
        logger.warning("板块池新鲜度写入失败（不影响解析）：%s", exc)
    return pool


async def resolve_sector_pool(
    db: AsyncSession | None,
    trade_date: date,
    *,
    boards: Iterable[dict[str, Any]] | None = None,
    refresh: bool = False,
) -> SectorPool:
    """解析目标池；``boards`` 参数仅用于测试/离线复用。

    ``db=None`` 时自建只读会话；返回的 ``actionable_codes`` 是唯一允许进入
    每日采集的板块集合。
    """
    key = _cache_key(trade_date)
    if not refresh and key in _CACHE:
        return _CACHE[key]
    if db is None:
        from app.db.session import session_scope

        async with session_scope() as session:
            return await _resolve_with_session(
                session, trade_date, boards=boards, key=key
            )
    return await _resolve_with_session(db, trade_date, boards=boards, key=key)


__all__ = [
    "SectorPool",
    "resolve_sector_pool",
    "clear_sector_pool_cache",
]
