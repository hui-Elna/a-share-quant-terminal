"""板块历史日线（V10）：只抓目标池，不透支非目标板块。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date

from app.core import config
from app.ingest import service, ths_board
from app.ingest.sector_pool import SectorPool

D = date(2026, 11, 23)


class _FakeJob:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def begin(self, *args, **kwargs) -> None:
        return None

    async def update(self, *args, **kwargs) -> None:
        return None

    async def finish(self, *args, **kwargs) -> None:
        return None


def test_sector_daily_only_fetches_target_pool(monkeypatch) -> None:
    captured: dict = {}

    @asynccontextmanager
    async def fake_lock(task):
        yield True

    @asynccontextmanager
    async def fake_scope(db):
        yield object()

    async def fake_trade_day(db, day):
        return True

    def fake_today():
        return D

    def fake_after_close():
        return True

    async def fake_pool(db, day):
        return SectorPool(
            trade_date=day,
            industry_codes=("THS_半导体", "THS_银行"),
            theme_codes=(),
            actionable_codes=("THS_半导体",),
            excluded={"THS_银行": "etf_illiquid"},
            version="test",
        )

    def fake_boards():
        return [
            {"sector_code": "THS_半导体", "sector_name": "半导体", "sector_type": "industry"},
            {"sector_code": "THS_银行", "sector_name": "银行", "sector_type": "industry"},
        ]

    async def fake_fetch(trade_date, boards=None, deadline_sec=None):
        captured["boards"] = [board["sector_code"] for board in boards]
        captured["deadline"] = deadline_sec
        return (
            [{"sector_code": "THS_半导体", "trade_date": trade_date}],
            {"source": "ths_hist", "failed": 0, "empty": 0, "boards": len(boards)},
        )

    async def fake_live(rows, trade_date, allowed_codes=None):
        return 0

    async def fake_quality(*args, **kwargs):
        return None

    async def fake_upsert(session, model, rows, keys, **kwargs):
        return len(rows)

    monkeypatch.setattr(config, "SECTOR_HISTORY_DAILY", True)
    monkeypatch.setattr(config, "SECTOR_HISTORY_SOFT_DEADLINE_SEC", 90.0)
    monkeypatch.setattr(service, "_advisory_lock", fake_lock)
    monkeypatch.setattr(service, "_db_scope", fake_scope)
    monkeypatch.setattr(service, "_is_trade_day", fake_trade_day)
    monkeypatch.setattr(service, "_today_cn", fake_today)
    monkeypatch.setattr(service, "_after_close_ready", fake_after_close)
    monkeypatch.setattr(service, "resolve_sector_pool", fake_pool)
    monkeypatch.setattr(service, "load_board_list", fake_boards)
    monkeypatch.setattr(service, "_JobTracker", _FakeJob)
    monkeypatch.setattr(service, "_fill_sector_rows_from_live", fake_live)
    monkeypatch.setattr(service, "record_quality", fake_quality)
    monkeypatch.setattr(service, "upsert_rows", fake_upsert)
    monkeypatch.setattr(ths_board, "fetch_daily_rows", fake_fetch)

    result = asyncio.run(service.collect_sector_daily(D, db=object()))

    assert captured["boards"] == ["THS_半导体"]
    assert captured["deadline"] == 90.0
    assert result.total == 1
    assert result.rows == 1
    assert result.detail["coverage"] == 1.0
