"""V8 W3：企业微信推送纯逻辑与重试测试（不联网）。"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.notify import push  # noqa: E402


def _sections() -> list[dict]:
    return [
        {
            "algo_code": "a3",
            "algo_name": "央企 × 综合评分TOP",
            "run_id": "run-a3",
            "signal_count": 2,
            "items": [
                {"rank": 1, "code": "601668", "name": "中国建筑", "summary": "评分 8.10"},
                {"rank": 2, "code": "600028", "name": "中国石化", "summary": "评分 7.90"},
            ],
        },
        {
            "algo_code": "a4",
            "algo_name": "高股息质量 × 低估值",
            "run_id": "run-a4",
            "signal_count": 0,
            "items": [],
        },
    ]


def test_build_message_contains_sections_and_no_signal() -> None:
    message = push.build_message(date(2026, 11, 13), _sections(), "http://127.0.0.1:5173/algo-center")
    assert "GFNA 选股播报 | 2026-11-13" in message
    assert "中国建筑(601668)" in message
    assert "无信号" in message
    assert "http://127.0.0.1:5173/algo-center" in message


def test_build_message_truncates_to_wecom_limit() -> None:
    sections = [
        {
            "algo_code": "c1",
            "algo_name": "海龟突破",
            "run_id": "run-c1",
            "signal_count": 1000,
            "items": [
                {"rank": index, "code": f"{600000 + index:06d}", "name": "股票", "summary": "x" * 100}
                for index in range(1000)
            ],
        }
    ]
    message = push.build_message(date(2026, 11, 13), sections)
    assert len(message.encode("utf-8")) <= push._MAX_BYTES  # noqa: SLF001
    assert "已截断）" in message
    assert message.endswith("条件复述，非投资建议")


def test_one_line_reason_from_triggered_and_breakdown() -> None:
    triggered = push._one_line_reason(  # noqa: SLF001
        {
            "metrics": {
                "triggered": [
                    {"label": "PE_TTM < 20", "passed": True, "actual": 12.3, "op": "lt", "threshold": 20},
                    {"label": "股息率 ≥ 4%", "passed": True, "actual": 5.1},
                    {"label": "未通过条件", "passed": False, "actual": 1},
                ]
            }
        }
    )
    assert "PE_TTM < 20" in triggered
    assert "股息率 ≥ 4%" in triggered
    assert "未通过条件" not in triggered

    breakdown = push._one_line_reason(  # noqa: SLF001
        {"metrics": {"score_breakdown": {"parts": [{"name": "价值", "score": 8.2}]}}}
    )
    assert "价值 8.2" in breakdown
    assert push._one_line_reason({"metrics": {}}) == "归因缺失"  # noqa: SLF001


def test_build_message_reason_and_disclaimer() -> None:
    sections = [
        {
            "algo_code": "a3",
            "algo_name": "央企 × 综合评分TOP",
            "run_id": "run-a3",
            "signal_count": 1,
            "items": [
                {
                    "rank": 1,
                    "code": "601668",
                    "name": "中国建筑",
                    "summary": "评分 8.10",
                    "metrics": {"triggered": [{"label": "综合评分 ≥ 6", "passed": True, "actual": 8.1}]},
                }
            ],
        }
    ]
    message = push.build_message(date(2026, 11, 13), sections)
    assert "综合评分 ≥ 6" in message
    assert "条件复述，非投资建议" in message
    for forbidden in ("建议买入", "值得买"):
        assert forbidden not in message


def test_build_message_marks_observed_candidates() -> None:
    sections = [
        {
            "algo_code": "c1",
            "algo_name": "海龟突破",
            "run_id": "run-c1",
            "signal_count": 1,
            "observed": True,
            "items": [{"rank": 1, "code": "600000", "name": "浦发银行", "summary": "突破"}],
        }
    ]
    message = push.build_message(date(2026, 11, 13), sections)
    assert "观察·未通过回测" in message


def test_summary_for_candidates() -> None:
    assert "股息 6.00%" in push._summary_for("a4", 6.0, {"dv_ttm": 6.0, "pe_ttm": 10.0})  # noqa: SLF001
    assert "流通市值" in push._summary_for("c1", 1.0, {"float_cap": 1.2e11})  # noqa: SLF001
    assert "RPS 95.0" in push._summary_for("c3", 95.0, {"rps": 95.0})  # noqa: SLF001
    assert "带宽分位 10.0%" in push._summary_for("c6", 10.0, {"boll_width_pct60": 10.0})  # noqa: SLF001


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {"errcode": 0, "errmsg": "ok"}

    def json(self) -> dict:
        return self._body


class _FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def _noop_sleep(seconds: float) -> None:
    return None


def test_post_wecom_retries_then_succeeds(monkeypatch) -> None:
    fake = _FakeClient([RuntimeError("boom"), RuntimeError("boom"), _FakeResponse()])
    monkeypatch.setattr(push.httpx, "AsyncClient", lambda **kwargs: fake)
    monkeypatch.setattr(push.asyncio, "sleep", _noop_sleep)

    attempts = asyncio.run(push.post_wecom_markdown("https://example.invalid/hook", "hi"))
    assert attempts == 3
    assert fake.calls == 3


def test_post_wecom_raises_after_three_failures(monkeypatch) -> None:
    fake = _FakeClient([RuntimeError("boom")])
    monkeypatch.setattr(push.httpx, "AsyncClient", lambda **kwargs: fake)
    monkeypatch.setattr(push.asyncio, "sleep", _noop_sleep)

    with pytest.raises(RuntimeError):
        asyncio.run(push.post_wecom_markdown("https://example.invalid/hook", "hi"))
    assert fake.calls == 3


def test_push_disabled_returns_without_db(monkeypatch) -> None:
    monkeypatch.setattr(push, "PUSH_ENABLED", False)
    monkeypatch.setattr(push, "PUSH_DRY_RUN", False)
    result = asyncio.run(push.push_daily_results(None))
    assert result["enabled"] is False
    assert result["sent"] == 0
