"""盘后链路数据重采限量的数据库集成测试。"""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from app import daily_chain as dc
from app.core import config
from app.db.models import IngestJob

TRADE_DATE = date(2026, 11, 24)


async def _add_job(db, status: str, checkpoint: dict) -> None:
    db.add(IngestJob(
        job_id=str(uuid4()),
        task=dc.CHAIN_TASK,
        dataset=dc.CHAIN_TASK,
        trade_date=TRADE_DATE,
        status=status,
        checkpoint=checkpoint,
    ))
    await db.commit()


@pytest.mark.asyncio
async def test_data_retry_limit_counts_only_failed_data_stage(
    db_session, monkeypatch
) -> None:
    monkeypatch.setattr(config, "DAILY_CHAIN_MAX_DATA_RETRIES", 1)

    assert await dc.failed_data_attempts(db_session, TRADE_DATE) == 0
    assert await dc.should_retry_data(db_session, TRADE_DATE) is True

    # 罗盘失败不算数据重采额度
    await _add_job(db_session, "failed", {"failed_stage": "compass"})
    assert await dc.failed_data_attempts(db_session, TRADE_DATE) == 0

    await _add_job(db_session, "failed", {"failed_stage": "data"})
    assert await dc.failed_data_attempts(db_session, TRADE_DATE) == 1
    assert await dc.should_retry_data(db_session, TRADE_DATE) is True

    await _add_job(db_session, "failed", {"failed_stage": "data"})
    assert await dc.failed_data_attempts(db_session, TRADE_DATE) == 2
    assert await dc.should_retry_data(db_session, TRADE_DATE) is False


@pytest.mark.asyncio
async def test_data_retry_disabled(db_session, monkeypatch) -> None:
    monkeypatch.setattr(config, "DAILY_CHAIN_MAX_DATA_RETRIES", 0)
    assert await dc.should_retry_data(db_session, TRADE_DATE) is False
