"""Tests for timeline alignment and lagged cross-correlation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.correlate import align, driver_correlation_matrix, lagged_cross_correlation


def _weekly_series(n: int, tz: str = "UTC") -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-07", periods=n, freq="W", tz=tz),
        "value": np.arange(n, dtype=float),
    })


def test_align_basic():
    df = _weekly_series(8)
    result = align({"signal": df})
    assert "signal" in result.columns
    assert len(result) == 8


def test_align_sums_multi_row_signal():
    """Transit has one row per route per week — align() must sum them."""
    timestamps = pd.date_range("2024-01-07", periods=4, freq="W", tz="UTC")
    df = pd.DataFrame({
        "timestamp": list(timestamps) * 2,
        "route": ["Red"] * 4 + ["Blue"] * 4,
        "value": [10.0] * 8,
    })
    result = align({"transit": df})
    assert result["transit"].iloc[0] == 20.0


def test_align_dst_boundary():
    """align() must not crash on the spring-forward week."""
    timestamps = pd.date_range(
        "2025-03-02", periods=8, freq="W", tz="America/New_York"
    )
    df = pd.DataFrame({"timestamp": timestamps, "value": range(8)})
    result = align({"signal": df})
    assert not result.empty


def test_align_skips_empty_signal():
    df = _weekly_series(4)
    result = align({"good": df, "empty": pd.DataFrame()})
    assert "good" in result.columns
    assert "empty" not in result.columns


def test_lagged_cross_correlation_detects_planted_lag():
    """A planted 2-week lag in a random series must be recovered exactly.

    Uses random (non-periodic) noise so the cross-correlation peaks sharply
    at lag=2 rather than spreading across nearby lags as a sine wave would.
    """
    rng = np.random.default_rng(42)
    n = 60
    t = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    base = pd.Series(rng.standard_normal(n), index=t)
    # response is base shifted forward by 2 weeks (driver leads by 2)
    response = base.shift(2).dropna()
    result = lagged_cross_correlation(
        base.loc[response.index], response, max_lag=6, deseasonalize=False
    )
    assert result.best_lag == 2
    assert result.best_corr > 0.99


def test_lagged_cross_correlation_insufficient_data():
    t = pd.date_range("2024-01-07", periods=5, freq="W", tz="UTC")
    s = pd.Series(range(5), index=t, dtype=float)
    with pytest.raises(ValueError, match="Not enough"):
        lagged_cross_correlation(s, s, max_lag=8)


def test_lagged_cross_correlation_deseasonalize():
    """Deseasonalized path runs without error and returns same shape."""
    t = pd.date_range("2024-01-07", periods=26, freq="W", tz="UTC")
    s = pd.Series(np.random.default_rng(0).normal(size=26), index=t)
    result = lagged_cross_correlation(s, s, max_lag=4, deseasonalize=True)
    assert len(result.lags) == 5
    assert len(result.correlations) == 5


def test_driver_correlation_matrix_detects_correlated_pair():
    """Two drivers built from the same noise must show up strongly correlated,
    while an independent third driver should not."""
    rng = np.random.default_rng(7)
    n = 60
    t = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    base = rng.standard_normal(n)
    aligned = pd.DataFrame({
        "a": base,
        "b": base + rng.normal(scale=0.01, size=n),
        "c": rng.standard_normal(n),
        "hospital_demand": rng.standard_normal(n),
    }, index=t)

    corr = driver_correlation_matrix(aligned, deseasonalize=False)
    assert "hospital_demand" not in corr.columns
    assert corr.loc["a", "b"] > 0.99
    assert abs(corr.loc["a", "c"]) < 0.5


def test_driver_correlation_matrix_diagonal_is_one():
    t = pd.date_range("2024-01-07", periods=30, freq="W", tz="UTC")
    aligned = pd.DataFrame({
        "a": np.arange(30, dtype=float),
        "b": np.arange(30, dtype=float) ** 2,
    }, index=t)
    corr = driver_correlation_matrix(aligned, deseasonalize=False)
    assert corr.loc["a", "a"] == pytest.approx(1.0)
    assert corr.loc["b", "b"] == pytest.approx(1.0)
