"""V8 W2 候选策略（c1–c3、c6）纯函数测试：窗口特征 + 策略掩码。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.errors import InvalidParamError  # noqa: E402
from app.strategy.strategies import (  # noqa: E402
    _validate_params,
    _window_features,
    rank_signals,
)


def _bars(**overrides) -> pd.DataFrame:
    count = len(next(iter(overrides.values()))) if overrides else 1
    base = {
        "open": [10.0] * count,
        "high": [10.0] * count,
        "low": [9.0] * count,
        "close": [10.0] * count,
        "volume": [100.0] * count,
        "amount": [1.0e8] * count,
        "adj_factor": [1.0] * count,
        "is_limit_up": [False] * count,
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_window_features_c1_breakout_and_insufficient_bars() -> None:
    # 60 日动量需要 61 根 K 线：60 根横盘 + 1 根突破
    high = [10.0] * 60 + [31.0]
    close = [10.0] * 60 + [31.0]
    bars = _bars(high=high, close=close, open=[10.0] * 61)
    features = _window_features(bars, "c1", 20)
    assert features is not None
    assert features["high20_prev"] == pytest.approx(10.0)
    assert features["adj_close"] == pytest.approx(31.0)
    assert features["prev_adj_close"] == pytest.approx(10.0)
    assert features["mom60"] == pytest.approx(2.1)
    assert features["roc20"] == pytest.approx(2.1)
    assert features["vol20"] > 0
    assert _window_features(bars.tail(60), "c1", 20) is None


def test_window_features_c2_high_tight_flag() -> None:
    high = [20.0] * 30 + [19.0] * 10
    low = [10.0] * 30 + [17.5] * 10
    volume = [100.0] * 39 + [50.0]
    bars = _bars(high=high, low=low, volume=volume)
    features = _window_features(bars, "c2", 40)
    assert features is not None
    assert features["range40_ratio"] == pytest.approx(2.0)
    assert features["high10"] == pytest.approx(19.0)
    assert features["low10"] == pytest.approx(17.5)
    assert features["vol_ma20_prev"] == pytest.approx(100.0)


def test_window_features_c3_rps_window() -> None:
    closes = np.linspace(10.0, 12.0, 121)
    bars = _bars(
        open=closes,
        close=closes,
        high=closes * 1.01,
        low=closes * 0.99,
    )
    features = _window_features(bars, "c3", 120)
    assert features is not None
    assert features["roc120"] == pytest.approx(0.2, abs=1e-9)
    assert features["high120"] == pytest.approx(12.12, abs=1e-6)
    assert _window_features(bars.tail(120), "c3", 120) is None


def _candidate_frame(codes: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": codes,
            "name": [f"股票{index}" for index in range(len(codes))],
            "close": [10.0] * len(codes),
            "pct_chg": [1.0] * len(codes),
            "industry_l1": ["电子"] * len(codes),
            "amount": [1.0e8] * len(codes),
            "amount_ma20": [1.0e8] * len(codes),
            "volume": [200.0] * len(codes),
            "float_cap": [1.0e9] * len(codes),
        }
    )


def test_rank_signals_c1_turtle() -> None:
    frame = _candidate_frame(["600001", "600002", "600003"])
    frame["float_cap"] = [3.0e9, 1.0e9, 2.0e9]
    frame["adj_close"] = [11.0, 12.0, 9.0]
    frame["adj_open"] = [10.0, 11.0, 8.0]
    frame["prev_adj_close"] = [10.0, 10.0, 9.5]
    frame["high20_prev"] = [10.0, 11.0, 8.5]
    frame["mom60"] = [0.10, 0.10, 0.10]
    frame["roc20"] = [0.10, 0.10, 0.10]
    frame["vol20"] = [0.02, 0.02, 0.02]
    frame["is_limit_up"] = [False, False, False]
    result = rank_signals("c1", frame, {})
    assert list(result["code"]) == ["600001", "600002"]
    assert result["float_cap"].is_monotonic_decreasing


def test_rank_signals_c2_high_tight_flag() -> None:
    frame = _candidate_frame(["600011", "600012"])
    frame["range40_ratio"] = [2.0, 1.2]
    frame["high40"] = [20.0, 20.0]
    frame["high10"] = [19.0, 19.0]
    frame["low10"] = [17.5, 15.0]
    frame["vol_ma20_prev"] = [100.0, 100.0]
    frame["volume"] = [40.0, 40.0]
    result = rank_signals("c2", frame, {})
    assert list(result["code"]) == ["600011"]


def test_rank_signals_c2_default_thresholds_tightened() -> None:
    """2026-11 默认值：收敛 1.12、缩量 0.5、20 日均额 1 亿三条件同时生效。"""
    frame = _candidate_frame(["600011"])
    frame["range40_ratio"] = [2.0]
    frame["high40"] = [20.0]
    frame["high10"] = [19.0]
    frame["low10"] = [16.8]  # 19 / 16.8 ≈ 1.131 > 1.12，收敛不足
    frame["vol_ma20_prev"] = [100.0]
    frame["volume"] = [55.0]  # 0.55 > 0.5，缩量不足
    frame["amount_ma20"] = [8.0e7]  # 低于 1 亿
    assert len(rank_signals("c2", frame, {})) == 0

    # 放宽到旧默认值后同一标的入选，证明失败确由三个新阈值导致
    relaxed = {"tight_max": 1.15, "shrink_max": 0.6, "min_amount": 5.0e7}
    assert list(rank_signals("c2", frame, relaxed)["code"]) == ["600011"]


def test_rank_signals_c3_rps_breakout() -> None:
    frame = _candidate_frame(["600021", "600022"])
    frame["rps"] = [95.0, 50.0]
    frame["roc120"] = [0.30, 0.10]
    frame["adj_close"] = [12.0, 11.0]
    frame["high120"] = [12.5, 12.0]
    frame["roc20"] = [0.03, 0.03]
    frame["vol20"] = [0.03, 0.03]
    result = rank_signals("c3", frame, {})
    assert list(result["code"]) == ["600021"]


def test_rank_signals_c6_boll_squeeze() -> None:
    frame = _candidate_frame(["600061", "600062"])
    frame["boll_width_pct60"] = [10.0, 40.0]
    frame["boll_upper"] = [9.8, 9.8]
    frame["high20_close"] = [9.9, 9.9]
    frame["vol_ma5"] = [100.0, 100.0]
    frame["volume"] = [160.0, 160.0]
    frame["range20_pct"] = [8.0, 8.0]
    frame["amount_ma20"] = [3.0e8, 3.0e8]
    market = {"available": True, "c6_ok": True}
    frame.attrs["market"] = market
    result = rank_signals("c6", frame, {})
    assert list(result["code"]) == ["600061"]

    blocked = frame.copy()
    blocked.attrs["market"] = {"available": True, "c6_ok": False}
    assert len(rank_signals("c6", blocked, {})) == 0


def test_candidate_backtest_params_accepts_candidates() -> None:
    """V8：候选策略回测参数收集不能再走 SPECS[algo_code]，否则 KeyError。"""
    from app.api.v1.backtest import _collect_strategy_params

    params = _collect_strategy_params(
        "c2", {"lookback": "40", "mom_min": "1.6", "high_level": "0.8"}
    )
    assert params["lookback"] == 40
    assert params["mom_min"] == pytest.approx(1.6)
    assert params["high_level"] == pytest.approx(0.8)


def test_candidate_param_bounds() -> None:
    assert _validate_params("c1", {"lookback": 30})["lookback"] == 30
    with pytest.raises(InvalidParamError):
        _validate_params("c1", {"lookback": 0})
    with pytest.raises(InvalidParamError):
        _validate_params("c2", {"mom_min": 0.5})
    with pytest.raises(InvalidParamError):
        _validate_params("c3", {"rps_threshold": 120})
    with pytest.raises(InvalidParamError):
        _validate_params("c6", {"squeeze_pct": 0})
