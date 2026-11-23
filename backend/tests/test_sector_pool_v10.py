"""板块目标池解析（V10）单元测试：不联网、不连库。"""
from __future__ import annotations

import asyncio
from datetime import date

from app.core import config
from app.ingest import sector_pool

D = date(2026, 11, 23)


def _boards() -> list[dict]:
    return [
        {"sector_code": "THS_半导体", "sector_name": "半导体", "sector_type": "industry", "ths_code": "881121"},
        {"sector_code": "THS_银行", "sector_name": "银行", "sector_type": "industry", "ths_code": "881155"},
        {"sector_code": "THS_人工智能", "sector_name": "人工智能", "sector_type": "concept", "ths_code": "885728"},
        {"sector_code": "THS_机器人", "sector_name": "机器人", "sector_type": "concept", "ths_code": "885517"},
    ]


def _patch(monkeypatch, members: dict[str, int] | None = None,
           etfs: dict[str, dict] | None = None) -> None:
    members = members or {
        "THS_半导体": 10, "THS_银行": 10,
        "THS_人工智能": 10, "THS_机器人": 10,
    }
    etfs = etfs or {
        "THS_半导体": {"etf_code": "512480", "avg_amount": 2e8, "aum": 2e9},
        "THS_银行": {"etf_code": "512800", "avg_amount": 2e8, "aum": 2e9},
        "THS_人工智能": {"etf_code": "159819", "avg_amount": 2e8, "aum": 2e9},
        "THS_机器人": {"etf_code": "562500", "avg_amount": 2e8, "aum": 2e9},
    }

    async def fake_members(db, codes, trade_date):
        return members

    async def fake_etfs(db, codes, trade_date):
        return etfs

    monkeypatch.setattr(sector_pool, "_member_counts", fake_members)
    monkeypatch.setattr(sector_pool, "_etf_metrics", fake_etfs)
    sector_pool.clear_sector_pool_cache()


def test_industry_mode_ignores_themes(monkeypatch) -> None:
    _patch(monkeypatch)
    monkeypatch.setattr(config, "SECTOR_UNIVERSE_MODE", "industry")
    monkeypatch.setattr(config, "SECTOR_THEME_WHITELIST", ("人工智能",))

    pool = asyncio.run(sector_pool.resolve_sector_pool(object(), D, boards=_boards(), refresh=True))

    assert pool.industry_codes == ("THS_半导体", "THS_银行")
    assert pool.theme_codes == ()
    assert set(pool.actionable_codes) == {"THS_半导体", "THS_银行"}


def test_industry_plus_theme_keeps_whitelisted_concept(monkeypatch) -> None:
    _patch(monkeypatch)
    monkeypatch.setattr(config, "SECTOR_UNIVERSE_MODE", "industry_plus_theme")
    monkeypatch.setattr(config, "SECTOR_THEME_WHITELIST", ("人工智能",))

    pool = asyncio.run(sector_pool.resolve_sector_pool(object(), D, boards=_boards(), refresh=True))

    assert pool.theme_codes == ("THS_人工智能",)
    assert "THS_人工智能" in pool.actionable_codes
    assert "THS_机器人" not in pool.actionable_codes


def test_actionability_exclusions(monkeypatch) -> None:
    _patch(
        monkeypatch,
        members={"THS_半导体": 10, "THS_银行": 3, "THS_人工智能": 10, "THS_机器人": 10},
        etfs={
            "THS_半导体": {"etf_code": "512480", "avg_amount": 2e8, "aum": 2e9},
            "THS_银行": {"etf_code": "512800", "avg_amount": 2e8, "aum": 2e9},
            "THS_机器人": {"etf_code": "562500", "avg_amount": 1e6, "aum": 2e9},
        },
    )
    monkeypatch.setattr(config, "SECTOR_UNIVERSE_MODE", "industry_plus_theme")
    monkeypatch.setattr(config, "SECTOR_THEME_WHITELIST", ("人工智能", "机器人"))

    pool = asyncio.run(sector_pool.resolve_sector_pool(object(), D, boards=_boards(), refresh=True))

    assert pool.excluded["THS_银行"] == "insufficient_members"
    assert pool.excluded["THS_人工智能"] == "no_etf"
    assert pool.excluded["THS_机器人"] == "etf_illiquid"
    assert pool.actionable_codes == ("THS_半导体",)


def test_missing_aum_does_not_exclude(monkeypatch) -> None:
    _patch(
        monkeypatch,
        etfs={
            "THS_半导体": {"etf_code": "512480", "avg_amount": 2e8, "aum": None},
            "THS_银行": {"etf_code": "512800", "avg_amount": 2e8, "aum": 2e9},
            "THS_人工智能": {"etf_code": "159819", "avg_amount": 2e8, "aum": 2e9},
            "THS_机器人": {"etf_code": "562500", "avg_amount": 2e8, "aum": 2e9},
        },
    )
    monkeypatch.setattr(config, "SECTOR_UNIVERSE_MODE", "industry_plus_theme")
    monkeypatch.setattr(config, "SECTOR_THEME_WHITELIST", ("人工智能", "机器人"))

    pool = asyncio.run(sector_pool.resolve_sector_pool(object(), D, boards=_boards(), refresh=True))

    assert "THS_半导体" in pool.actionable_codes


def test_version_changes_when_pool_changes(monkeypatch) -> None:
    _patch(monkeypatch)
    monkeypatch.setattr(config, "SECTOR_UNIVERSE_MODE", "industry")
    monkeypatch.setattr(config, "SECTOR_THEME_WHITELIST", ())

    first = asyncio.run(sector_pool.resolve_sector_pool(object(), D, boards=_boards(), refresh=True))
    second = asyncio.run(sector_pool.resolve_sector_pool(object(), D, boards=_boards(), refresh=True))
    assert first.version == second.version

    _patch(monkeypatch, members={"THS_半导体": 10, "THS_银行": 3})
    third = asyncio.run(sector_pool.resolve_sector_pool(object(), D, boards=_boards(), refresh=True))
    assert third.version != first.version
