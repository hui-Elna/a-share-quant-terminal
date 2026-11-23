"""估值新鲜度跳过（V10）：同日重复调用不重算，force 可强制重算。"""
from __future__ import annotations

import asyncio
from datetime import date

from app.core import config
from app.ingest import valuation
from app.ingest.base import FetchResult

D = date(2026, 11, 23)


def _ok(rows: int = 0) -> FetchResult:
    return FetchResult(rows=rows, total=rows, elapsed_ms=1, quality_status="ok", detail={})


def test_fresh_valuation_skips_all_work(monkeypatch) -> None:
    async def fake_fresh(db, target):
        return True, 321

    def boom(*args, **kwargs):
        raise AssertionError("fresh 时不应执行 sync/compute")

    monkeypatch.setattr(config, "VALUATION_SKIP_IF_FRESH", True)
    monkeypatch.setattr(valuation, "_valuation_is_fresh", fake_fresh)
    monkeypatch.setattr(valuation, "sync_financials", boom)
    monkeypatch.setattr(valuation, "sync_dividends", boom)
    monkeypatch.setattr(valuation, "sync_revenue_bulk", boom)
    monkeypatch.setattr(valuation, "compute_valuation", boom)

    result = asyncio.run(valuation.collect_latest_valuation(D, db=object()))

    assert result.rows == 0
    assert result.detail["skipped"] == "already_collected"
    assert result.detail["existing"] == 321


def test_force_recomputes_even_when_fresh(monkeypatch) -> None:
    async def fake_fresh(db, target):
        raise AssertionError("force 时不应查询新鲜度")

    calls: list[str] = []

    async def fake_sync(*args, **kwargs):
        calls.append("sync")
        return _ok()

    async def fake_dividends(*args, **kwargs):
        calls.append("dividends")
        return _ok()

    async def fake_revenue(*args, **kwargs):
        calls.append("revenue")
        return _ok()

    async def fake_compute(*args, **kwargs):
        calls.append("compute")
        return _ok(rows=5)

    monkeypatch.setattr(config, "VALUATION_SKIP_IF_FRESH", True)
    monkeypatch.setattr(valuation, "_valuation_is_fresh", fake_fresh)
    monkeypatch.setattr(valuation, "sync_financials", fake_sync)
    monkeypatch.setattr(valuation, "sync_dividends", fake_dividends)
    monkeypatch.setattr(valuation, "sync_revenue_bulk", fake_revenue)
    monkeypatch.setattr(valuation, "compute_valuation", fake_compute)

    result = asyncio.run(valuation.collect_latest_valuation(D, db=object(), force=True))

    assert calls == ["sync", "dividends", "revenue", "compute"]
    assert result.rows == 5
