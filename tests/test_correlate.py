"""Tests for timeline alignment and lagged cross-correlation."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.analysis import correlate
from src.analysis.correlate import align, driver_correlation_matrix, lagged_cross_correlation, scan_drivers


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
    assert result.best_pvalue < 0.001
    assert result.ci_low[result.best_lag] > 0
    assert result.ambiguous is False


def test_lagged_cross_correlation_reports_pvalue_and_ci_shapes():
    rng = np.random.default_rng(1)
    n = 40
    t = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    s = pd.Series(rng.standard_normal(n), index=t)
    result = lagged_cross_correlation(s, s, max_lag=5, deseasonalize=False)
    for field in (result.pvalues, result.ci_low, result.ci_high, result.n_obs):
        assert len(field) == len(result.lags)


def test_lagged_cross_correlation_ci_excludes_zero_for_strong_imperfect_signal():
    """A strong but non-exact correlation (unlike an exact-copy shift, which
    hits the r=1 singularity) should give a well-defined CI that excludes zero."""
    rng = np.random.default_rng(42)
    n = 200
    t = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    base = pd.Series(rng.standard_normal(n), index=t)
    noise = pd.Series(rng.normal(scale=0.1, size=n), index=t)
    response = (base.shift(2) + noise).dropna()
    result = lagged_cross_correlation(
        base.loc[response.index], response, max_lag=6, deseasonalize=False
    )
    assert result.best_lag == 2
    assert 0 < result.best_corr < 1
    assert result.ci_low[result.best_lag] > 0
    assert result.ambiguous is False


def test_lagged_cross_correlation_flags_ambiguous_for_pure_noise():
    """With no real relationship, the 'best' lag by |corr| is just noise —
    it must not be reported as clearly distinguishable from its runner-up,
    even when its own p-value looks nominally significant on its own."""
    rng = np.random.default_rng(3)
    n = 60
    t = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    driver = pd.Series(rng.standard_normal(n), index=t)
    response = pd.Series(rng.standard_normal(n), index=t)
    result = lagged_cross_correlation(driver, response, max_lag=6, deseasonalize=False)
    assert result.ambiguous is True


def test_fisher_ci_narrows_with_more_data():
    lo_small, hi_small = correlate._fisher_ci(0.5, n=10)
    lo_large, hi_large = correlate._fisher_ci(0.5, n=200)
    assert (hi_small - lo_small) > (hi_large - lo_large)


def test_fisher_ci_undefined_for_tiny_n():
    lo, hi = correlate._fisher_ci(0.5, n=3)
    assert math.isnan(lo) and math.isnan(hi)


def test_fisher_ci_clamps_near_perfect_r_instead_of_nan():
    """r=1 is a genuine transform singularity, but clamping just inside the
    domain keeps a perfect/near-perfect correlation's CI well-defined (tight,
    not NaN) — NaN here would wrongly read as "no significance info available"
    for what is actually the most significant possible result."""
    lo, hi = correlate._fisher_ci(1.0, n=50)
    assert not math.isnan(lo) and not math.isnan(hi)
    assert lo > 0.9
    assert hi <= 1.0


def test_is_ambiguous_true_for_overlapping_cis():
    assert correlate._is_ambiguous(
        ci_low=[0.1, 0.05], ci_high=[0.5, 0.45], order=[0, 1]
    ) is True


def test_is_ambiguous_false_for_clearly_separated_cis():
    assert correlate._is_ambiguous(
        ci_low=[0.8, -0.1], ci_high=[0.95, 0.1], order=[0, 1]
    ) is False


def test_is_ambiguous_true_when_ci_undefined():
    assert correlate._is_ambiguous(
        ci_low=[float("nan"), 0.1], ci_high=[float("nan"), 0.3], order=[0, 1]
    ) is True


def test_is_ambiguous_false_for_single_lag():
    assert correlate._is_ambiguous(ci_low=[0.1], ci_high=[0.5], order=[0]) is False


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


def test_scan_drivers_runs_every_driver_against_the_response():
    rng = np.random.default_rng(11)
    n = 60
    t = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    aligned = pd.DataFrame({
        "a": rng.standard_normal(n),
        "b": rng.standard_normal(n),
        "hospital_demand": rng.standard_normal(n),
    }, index=t)
    results = scan_drivers(aligned, "hospital_demand", max_lag=4, deseasonalize=False)
    assert set(results.keys()) == {"a", "b"}
    for result in results.values():
        assert len(result.lags) == 5


def test_scan_drivers_defaults_to_every_non_response_column():
    rng = np.random.default_rng(12)
    n = 60
    t = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    aligned = pd.DataFrame({
        "a": rng.standard_normal(n),
        "b": rng.standard_normal(n),
        "c": rng.standard_normal(n),
        "hospital_demand": rng.standard_normal(n),
    }, index=t)
    results = scan_drivers(aligned, "hospital_demand", max_lag=3, deseasonalize=False)
    assert set(results.keys()) == {"a", "b", "c"}


def test_scan_drivers_skips_drivers_with_too_little_data():
    """A driver with too few overlapping points to test (ValueError from
    lagged_cross_correlation) is silently omitted, not a scan failure."""
    rng = np.random.default_rng(13)
    n = 60
    t = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    short_t = t[:3]
    aligned = pd.DataFrame({
        "good": pd.Series(rng.standard_normal(n), index=t),
        "too_short": pd.Series(rng.standard_normal(3), index=short_t),
        "hospital_demand": pd.Series(rng.standard_normal(n), index=t),
    })
    results = scan_drivers(aligned, "hospital_demand", max_lag=4, deseasonalize=False)
    assert "good" in results
    assert "too_short" not in results
