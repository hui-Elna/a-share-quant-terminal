"""盘后串行链路：顺序、门禁与告警的纯逻辑测试。"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app import daily_chain as dc

TRADE_DATE = date(2026, 11, 24)


class FakeDB:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _patch_runtime(monkeypatch, *, trade_day: bool = True) -> list[tuple]:
    alerts: list[tuple] = []

    async def fake_trade_day(db, day):
        return trade_day

    async def fake_lock(db, key):
        return True

    async def fake_unlock(db, key):
        return None

    async def fake_start(db, trade_date, reason, run_data):
        return object()

    async def fake_finish(db, job, summary):
        return None

    async def fake_alert(db, trade_date, exc):
        alerts.append((trade_date, exc.stage, str(exc)))

    monkeypatch.setattr("app.ingest.calendar.is_trade_day", fake_trade_day)
    monkeypatch.setattr(dc, "try_advisory_lock", fake_lock)
    monkeypatch.setattr(dc, "advisory_unlock", fake_unlock)
    monkeypatch.setattr(dc, "_start_job", fake_start)
    monkeypatch.setattr(dc, "_finish_job", fake_finish)
    monkeypatch.setattr(dc, "_alert", fake_alert)
    return alerts


def test_data_failure_blocks_compass_and_push(monkeypatch) -> None:
    alerts = _patch_runtime(monkeypatch)
    called: list[str] = []

    async def fail_data(db, trade_date, *, run_data):
        raise dc.ChainStageError(
            "data", "数据未就绪", detail={"missing_algos": ["a3"]}
        )

    async def compass(db, trade_date):
        called.append("compass")
        return {"status": "success"}

    async def push(db, trade_date, card_id):
        called.append("push")
        return {"status": "success"}

    monkeypatch.setattr(dc, "_run_data_stage", fail_data)
    monkeypatch.setattr(dc, "_run_compass_stage", compass)
    monkeypatch.setattr(dc, "_run_push_stage", push)

    result = asyncio.run(dc.run_daily_chain(FakeDB(), TRADE_DATE, run_data=True))

    assert result["status"] == "failed"
    assert result["failed_stage"] == "data"
    assert called == []
    assert alerts and alerts[0][1] == "data"


def test_compass_failure_blocks_push(monkeypatch) -> None:
    alerts = _patch_runtime(monkeypatch)
    called: list[str] = []

    async def ok_data(db, trade_date, *, run_data):
        return {"status": "skipped", "reason": "already_ready"}

    async def fail_compass(db, trade_date):
        called.append("compass")
        raise dc.ChainStageError("compass", "卡片无效")

    async def push(db, trade_date, card_id):
        called.append("push")
        return {"status": "success"}

    monkeypatch.setattr(dc, "_run_data_stage", ok_data)
    monkeypatch.setattr(dc, "_run_compass_stage", fail_compass)
    monkeypatch.setattr(dc, "_run_push_stage", push)

    result = asyncio.run(dc.run_daily_chain(FakeDB(), TRADE_DATE, run_data=False))

    assert result["status"] == "failed"
    assert result["failed_stage"] == "compass"
    assert called == ["compass"]
    assert alerts and alerts[0][1] == "compass"


def test_chain_runs_data_compass_push_in_order(monkeypatch) -> None:
    alerts = _patch_runtime(monkeypatch)
    calls: list[str] = []
    pushed_card: list[str | None] = []

    async def data(db, trade_date, *, run_data):
        calls.append("data")
        return {"status": "success"}

    async def compass(db, trade_date):
        calls.append("compass")
        return {"status": "success", "card_id": "card-1", "exec_cap": 30}

    async def push(db, trade_date, card_id):
        calls.append("push")
        pushed_card.append(card_id)
        return {"status": "success"}

    monkeypatch.setattr(dc, "_run_data_stage", data)
    monkeypatch.setattr(dc, "_run_compass_stage", compass)
    monkeypatch.setattr(dc, "_run_push_stage", push)

    result = asyncio.run(dc.run_daily_chain(FakeDB(), TRADE_DATE, run_data=True))

    assert result["status"] == "success"
    assert calls == ["data", "compass", "push"]
    assert pushed_card == ["card-1"]
    assert alerts == []


def test_resume_without_data_is_blocked_and_silent(monkeypatch) -> None:
    alerts = _patch_runtime(monkeypatch)
    called: list[str] = []

    async def blocked_data(db, trade_date, *, run_data):
        raise dc.ChainStageError(
            "data", "数据尚未完成", blocked=True
        )

    async def compass(db, trade_date):
        called.append("compass")
        return {"status": "success"}

    monkeypatch.setattr(dc, "_run_data_stage", blocked_data)
    monkeypatch.setattr(dc, "_run_compass_stage", compass)
    monkeypatch.setattr(dc, "_run_push_stage", compass)

    result = asyncio.run(dc.run_daily_chain(FakeDB(), TRADE_DATE, run_data=False))

    assert result["status"] == "blocked"
    assert result["job_status"] == "skipped"
    assert called == []
    assert alerts == []


def test_unexpected_exception_marks_failed_and_alerts(monkeypatch) -> None:
    alerts = _patch_runtime(monkeypatch)

    async def data(db, trade_date, *, run_data):
        return {"status": "success"}

    async def compass(db, trade_date):
        raise RuntimeError("boom")

    monkeypatch.setattr(dc, "_run_data_stage", data)
    monkeypatch.setattr(dc, "_run_compass_stage", compass)

    result = asyncio.run(dc.run_daily_chain(FakeDB(), TRADE_DATE, run_data=False))

    assert result["status"] == "failed"
    assert result["failed_stage"] == "compass"
    assert "RuntimeError" in result["error"]
    assert alerts and alerts[0][1] == "compass"


def test_non_trade_day_is_skipped(monkeypatch) -> None:
    _patch_runtime(monkeypatch, trade_day=False)
    result = asyncio.run(dc.run_daily_chain(FakeDB(), TRADE_DATE))
    assert result["status"] == "skipped"
    assert result["reason"] == "not_trade_day"


def test_lock_busy_is_skipped(monkeypatch) -> None:
    _patch_runtime(monkeypatch)

    async def busy(db, key):
        return False

    monkeypatch.setattr(dc, "try_advisory_lock", busy)
    result = asyncio.run(dc.run_daily_chain(FakeDB(), TRADE_DATE))
    assert result["status"] == "skipped"
    assert result["reason"] == "lock_busy"


class _Card:
    def __init__(self, **kwargs) -> None:
        self.trade_date = kwargs.get("trade_date", TRADE_DATE)
        self.status = kwargs.get("status", "generated")
        self.exec_cap = kwargs.get("exec_cap", 30)
        self.candidates = kwargs.get("candidates", [])
        self.card_id = "card-1"


@pytest.mark.parametrize(
    ("card", "ok"),
    [
        (None, False),
        (_Card(trade_date=date(2026, 11, 21)), False),
        (_Card(status="draft"), False),
        (_Card(exec_cap=None), False),
        (_Card(candidates=None), False),
        (_Card(), True),
        (_Card(exec_cap=0), True),
    ],
)
def test_validate_card(card, ok) -> None:
    assert dc.validate_card(card, TRADE_DATE)["ok"] is ok


def test_expected_push_algos_ignores_unknown_codes(monkeypatch) -> None:
    monkeypatch.setattr(dc.config, "PUSH_ALGOS", ("a3", "zzz"))
    assert dc.expected_push_algos() == ["a3"]
    monkeypatch.setattr(dc.config, "PUSH_ALGOS", ())
    assert dc.expected_push_algos() == list(dc.SPECS)


def test_non_required_step_failure_does_not_block(monkeypatch) -> None:
    """两融 T+1 未披露等非必需步骤失败，不应阻断罗盘与推送。"""

    async def fake_pipeline(trade_date, db):
        return {
            "steps": {
                "snapshot": {"status": "success"},
                "features": {"status": "success"},
                "scores": {"status": "success"},
                "sector_scores": {"status": "success"},
                "temperature": {"status": "success"},
                "regime": {"status": "success"},
                "algos": {"status": "success"},
                "margin": {"status": "failed", "error": "两融尚未披露"},
            }
        }

    async def ready(db, trade_date):
        return True, {"missing_datasets": [], "missing_algos": []}

    async def not_ready(db, trade_date):
        return False, {"missing_datasets": [], "missing_algos": []}

    calls = {"n": 0}

    async def readiness(db, trade_date):
        calls["n"] += 1
        return await (not_ready(db, trade_date) if calls["n"] == 1 else ready(db, trade_date))

    monkeypatch.setattr(dc, "run_daily_pipeline", fake_pipeline)
    monkeypatch.setattr(dc, "data_readiness", readiness)

    result = asyncio.run(dc._run_data_stage(FakeDB(), TRADE_DATE, run_data=True))

    assert result["status"] == "success"


def test_required_step_failure_blocks_data_stage(monkeypatch) -> None:
    async def fake_pipeline(trade_date, db):
        return {
            "steps": {
                "snapshot": {"status": "success"},
                "features": {"status": "failed", "error": "boom"},
                "algos": {"status": "success"},
            }
        }

    async def readiness(db, trade_date):
        return False, {"missing_datasets": [{"dataset": "stock_features"}]}

    monkeypatch.setattr(dc, "run_daily_pipeline", fake_pipeline)
    monkeypatch.setattr(dc, "data_readiness", readiness)

    with pytest.raises(dc.ChainStageError) as exc_info:
        asyncio.run(dc._run_data_stage(FakeDB(), TRADE_DATE, run_data=True))

    assert exc_info.value.stage == "data"
    assert "features" in exc_info.value.detail["failed_steps"]


def test_compass_stage_skips_existing_valid_card(monkeypatch) -> None:
    async def fake_find_card(db, trade_date):
        return _Card()

    monkeypatch.setattr(dc, "find_card", fake_find_card)

    result = asyncio.run(dc._run_compass_stage(FakeDB(), TRADE_DATE))

    assert result["status"] == "skipped"
    assert result["reason"] == "already_generated"
    assert result["card_id"] == "card-1"


def test_push_stage_stock_failure_still_pushes_compass(monkeypatch) -> None:
    called: list[str] = []

    async def fake_push(db, trade_date):
        called.append("stock")
        return {"error": "webhook 未配置"}

    async def fake_card_push(db, card_id):
        called.append("compass")
        return {"status": "sent", "card_id": card_id}

    monkeypatch.setattr("app.notify.push.push_daily_results", fake_push)
    monkeypatch.setattr("app.compass.push.send_card_push", fake_card_push)
    monkeypatch.setattr(dc.config, "PUSH_OBSERVATION", False)

    with pytest.raises(dc.ChainStageError) as exc_info:
        asyncio.run(dc._run_push_stage(FakeDB(), TRADE_DATE, "card-1"))

    assert exc_info.value.stage == "push"
    assert called == ["stock", "compass"]
    assert "选股推送失败" in str(exc_info.value)


def test_push_stage_disabled_push_is_success(monkeypatch) -> None:
    async def fake_push(db, trade_date):
        return {"enabled": False, "sent": 0}

    async def fake_card_push(db, card_id):
        return {"status": "skipped", "reason": "already_sent"}

    monkeypatch.setattr("app.notify.push.push_daily_results", fake_push)
    monkeypatch.setattr("app.compass.push.send_card_push", fake_card_push)
    monkeypatch.setattr(dc.config, "PUSH_OBSERVATION", False)

    result = asyncio.run(dc._run_push_stage(FakeDB(), TRADE_DATE, "card-1"))

    assert result["status"] == "success"


def test_worker_pipeline_job_skips_non_trade_day(monkeypatch) -> None:
    from app import worker

    called: list[dict] = []

    async def fake_trade_day(db, day):
        return False

    async def fake_chain(*args, **kwargs):
        called.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr("app.ingest.calendar.is_trade_day", fake_trade_day)
    monkeypatch.setattr("app.daily_chain.run_daily_chain", fake_chain)

    asyncio.run(worker.run_daily_pipeline_job(None))

    assert called == []


def test_worker_pipeline_job_runs_chain_with_data(monkeypatch) -> None:
    from app import worker

    called: list[dict] = []

    async def fake_trade_day(db, day):
        return True

    async def fake_chain(*args, **kwargs):
        called.append(kwargs)
        return {"status": "success", "stages": {"data": {}}}

    monkeypatch.setattr("app.ingest.calendar.is_trade_day", fake_trade_day)
    monkeypatch.setattr("app.daily_chain.run_daily_chain", fake_chain)

    asyncio.run(worker.run_daily_pipeline_job(None))

    assert called and called[0]["run_data"] is True


def test_worker_watchdog_skips_while_pipeline_running(monkeypatch) -> None:
    from app import worker

    called: list[dict] = []

    async def busy(db, key):
        return False

    async def fake_chain(*args, **kwargs):
        called.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr("app.pipeline.try_advisory_lock", busy)
    monkeypatch.setattr("app.daily_chain.run_daily_chain", fake_chain)

    asyncio.run(worker.run_daily_chain_watchdog_job(FakeDB()))

    assert called == []


def test_worker_watchdog_resumes_with_alert_on_blocked(monkeypatch) -> None:
    from app import worker

    called: list[dict] = []

    async def free(db, key):
        return True

    async def unlock(db, key):
        return None

    async def fake_resolve(db):
        return TRADE_DATE

    async def ready(db, trade_date):
        return True, {}

    async def no_retry(db, trade_date):
        return False

    async def fake_chain(*args, **kwargs):
        called.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr("app.pipeline.try_advisory_lock", free)
    monkeypatch.setattr("app.pipeline.advisory_unlock", unlock)
    monkeypatch.setattr("app.daily_chain.resolve_chain_date", fake_resolve)
    monkeypatch.setattr("app.daily_chain.data_readiness", ready)
    monkeypatch.setattr("app.daily_chain.should_retry_data", no_retry)
    monkeypatch.setattr("app.daily_chain.run_daily_chain", fake_chain)

    asyncio.run(worker.run_daily_chain_watchdog_job(FakeDB()))

    assert called
    assert called[0]["run_data"] is False
    assert called[0]["notify_on_blocked"] is True


def test_worker_watchdog_retries_data_when_quota_left(monkeypatch) -> None:
    from app import worker

    called: list[dict] = []

    async def free(db, key):
        return True

    async def unlock(db, key):
        return None

    async def fake_resolve(db):
        return TRADE_DATE

    async def not_ready(db, trade_date):
        return False, {"missing_algos": ["a3"]}

    async def allow_retry(db, trade_date):
        return True

    async def fake_chain(*args, **kwargs):
        called.append(kwargs)
        return {"status": "failed"}

    monkeypatch.setattr("app.pipeline.try_advisory_lock", free)
    monkeypatch.setattr("app.pipeline.advisory_unlock", unlock)
    monkeypatch.setattr("app.daily_chain.resolve_chain_date", fake_resolve)
    monkeypatch.setattr("app.daily_chain.data_readiness", not_ready)
    monkeypatch.setattr("app.daily_chain.should_retry_data", allow_retry)
    monkeypatch.setattr("app.daily_chain.run_daily_chain", fake_chain)

    asyncio.run(worker.run_daily_chain_watchdog_job(FakeDB()))

    assert called
    assert called[0]["run_data"] is True
    assert called[0]["reason"] == "watchdog_retry"
    assert called[0]["notify_on_blocked"] is False


def test_worker_watchdog_alerts_when_retry_quota_exhausted(monkeypatch) -> None:
    from app import worker

    called: list[dict] = []

    async def free(db, key):
        return True

    async def unlock(db, key):
        return None

    async def fake_resolve(db):
        return TRADE_DATE

    async def not_ready(db, trade_date):
        return False, {}

    async def deny_retry(db, trade_date):
        return False

    async def fake_chain(*args, **kwargs):
        called.append(kwargs)
        return {"status": "blocked"}

    monkeypatch.setattr("app.pipeline.try_advisory_lock", free)
    monkeypatch.setattr("app.pipeline.advisory_unlock", unlock)
    monkeypatch.setattr("app.daily_chain.resolve_chain_date", fake_resolve)
    monkeypatch.setattr("app.daily_chain.data_readiness", not_ready)
    monkeypatch.setattr("app.daily_chain.should_retry_data", deny_retry)
    monkeypatch.setattr("app.daily_chain.run_daily_chain", fake_chain)

    asyncio.run(worker.run_daily_chain_watchdog_job(FakeDB()))

    assert called
    assert called[0]["run_data"] is False
    assert called[0]["notify_on_blocked"] is True


def test_alert_message_contains_stage_and_missing_detail() -> None:
    from app.notify.alerts import build_chain_alert_message

    message = build_chain_alert_message(
        TRADE_DATE,
        "data",
        "数据采集或入库校验未通过",
        blocked=["仓位罗盘分析", "机器人推送"],
        detail={
            "missing_datasets": [{"dataset": "stock_daily", "step": "snapshot"}],
            "missing_algos": ["a3"],
        },
    )
    assert "数据采集与入库校验" in message
    assert "stock_daily(snapshot)" in message
    assert "a3" in message
    assert "仓位罗盘分析" in message
    assert "不构成投资建议" in message

