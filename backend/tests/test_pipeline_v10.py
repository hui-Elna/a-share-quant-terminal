"""V10 编排：资金流周更跳过与 meta_data_quality 新鲜度判断。"""
from __future__ import annotations

import asyncio
from datetime import date

from app import pipeline
from app.core import config
from app.ingest import service

D = date(2026, 11, 23)


def _patch_resolve(monkeypatch) -> None:
    async def fake_resolve(db, trade_date=None):
        return D, True

    monkeypatch.setattr(pipeline, "resolve_trade_date", fake_resolve)


def test_moneyflow_daily_skips_when_weekly(monkeypatch) -> None:
    _patch_resolve(monkeypatch)
    monkeypatch.setattr(config, "MONEYFLOW_FULL_WEEKLY", True)

    result = asyncio.run(pipeline._dispatch(
        "moneyflow", trade_date=D, start=None, end=None,
        codes=None, force=False, db=object(),
    ))

    assert result["skipped"] is True
    assert result["reason"] == "weekly_only"


def test_moneyflow_force_runs_weekly(monkeypatch) -> None:
    _patch_resolve(monkeypatch)
    monkeypatch.setattr(config, "MONEYFLOW_FULL_WEEKLY", True)

    async def fake_collect(trade_date, db=None):
        return {"rows": 7}

    monkeypatch.setattr(service, "collect_moneyflow", fake_collect)

    result = asyncio.run(pipeline._dispatch(
        "moneyflow", trade_date=D, start=None, end=None,
        codes=None, force=True, db=object(),
    ))

    assert result == {"rows": 7}


def test_dataset_fresh_true_and_false() -> None:
    class _Result:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _DB:
        def __init__(self, row):
            self._row = row

        async def execute(self, *args, **kwargs):
            return _Result(self._row)

    assert asyncio.run(pipeline._dataset_fresh(_DB(("ok", D)), "valuation", D)) is True
    assert asyncio.run(pipeline._dataset_fresh(_DB(("stale", D)), "valuation", D)) is False
    assert asyncio.run(pipeline._dataset_fresh(_DB(None), "valuation", D)) is False
