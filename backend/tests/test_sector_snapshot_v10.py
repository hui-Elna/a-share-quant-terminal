"""板块即时快照（V10）单元测试：目标池过滤与 NULL 保护。"""
from __future__ import annotations

import asyncio
from datetime import date

import pandas as pd

from app.ingest import service, ths_sector
from app.ingest.sector_snapshot import _SNAPSHOT_UPDATE_COLUMNS, _snapshot_rows

D = date(2026, 11, 23)


def _frame() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "name": "半导体", "sector_type": "industry", "index_value": 1000.0,
            "pct_chg": 2.0, "leading_stock": "中芯国际", "leading_stock_pct": 5.0,
            "net_inflow": 1.2e8,
        },
        {
            "name": "银行", "sector_type": "industry", "index_value": 800.0,
            "pct_chg": 0.5, "leading_stock": "招商银行", "leading_stock_pct": 1.0,
            "net_inflow": 3.0e7,
        },
    ])


def test_snapshot_rows_only_target_and_no_ohlcv() -> None:
    rows = _snapshot_rows(_frame(), {"THS_半导体"}, D)

    assert len(rows) == 1
    row = rows[0]
    assert row["sector_code"] == "THS_半导体"
    assert row["pct_chg"] == 2.0
    assert row["main_net_inflow"] == 1.2e8
    assert row["src"] == "ths_snapshot"
    for column in (
        "open", "high", "low", "close", "pre_close", "amplitude",
        "volume", "amount", "turnover_rate",
    ):
        assert row[column] is None, column


def test_snapshot_update_columns_never_touch_ohlcv() -> None:
    forbidden = {
        "open", "high", "low", "close", "pre_close", "amplitude",
        "volume", "amount", "turnover_rate",
    }
    assert not (set(_SNAPSHOT_UPDATE_COLUMNS) & forbidden)


def test_live_fill_respects_allowed_codes(monkeypatch) -> None:
    async def fake_fetch():
        return _frame(), pd.DataFrame()

    def fake_build(frame, code_map, trade_date):
        return [
            {
                "sector_code": f"THS_{record['name']}",
                "sector_name": record["name"],
                "trade_date": trade_date,
            }
            for record in frame.to_dict("records")
        ]

    monkeypatch.setattr(ths_sector, "fetch_sector_fundflow", fake_fetch)
    monkeypatch.setattr(ths_sector, "build_sector_daily_rows", fake_build)

    rows: list[dict] = []
    filled = asyncio.run(service._fill_sector_rows_from_live(
        rows, D, allowed_codes={"THS_半导体"}
    ))

    assert filled == 1
    assert [row["sector_code"] for row in rows] == ["THS_半导体"]
