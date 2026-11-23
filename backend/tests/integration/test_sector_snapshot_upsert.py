"""板块快照专用 upsert：只更新快照字段，保护历史 OHLCV。"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models.fact import FactSectorDaily
from app.ingest.sector_snapshot import _upsert_snapshot_rows

pytestmark = pytest.mark.asyncio

D = date(2026, 11, 23)
CODE = "THS_TEST_SNAPSHOT"


async def test_snapshot_upsert_preserves_ohlcv(db_session) -> None:
    db_session.add(FactSectorDaily(
        sector_code=CODE, trade_date=D, sector_name="快照测试", sector_type="industry",
        open=10.0, high=11.0, low=9.5, close=10.5, pre_close=10.0,
        volume=1000, amount=2000.0, turnover_rate=1.2, src="test",
    ))
    await db_session.flush()

    await _upsert_snapshot_rows(db_session, [{
        "sector_code": CODE, "trade_date": D, "sector_name": "快照测试",
        "sector_type": "industry", "open": None, "high": None, "low": None,
        "close": None, "pre_close": None, "amplitude": None, "volume": None,
        "amount": None, "turnover_rate": None, "pct_chg": 2.0,
        "up_count": None, "down_count": None, "leading_stock": "测试股",
        "leading_stock_pct": 5.0, "main_net_inflow": 123.0,
        "src": "ths_snapshot", "updated_at": datetime.now(timezone.utc),
    }])

    row = (await db_session.execute(
        select(FactSectorDaily).where(
            FactSectorDaily.sector_code == CODE,
            FactSectorDaily.trade_date == D,
        )
    )).scalar_one()
    assert (row.open, row.high, row.low, row.close) == (10.0, 11.0, 9.5, 10.5)
    assert (row.volume, row.amount) == (1000, 2000.0)
    assert row.pct_chg == 2.0
    assert row.main_net_inflow == 123.0
    assert row.leading_stock == "测试股"
    assert row.src == "ths_snapshot"
