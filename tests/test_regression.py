"""Tests for the multi-driver lagged count regression."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.regression import build_lagged_design_matrix, fit_count_regression


def _planted_aligned(n: int = 80, lag: int = 2, beta: float = 0.8, seed: int = 0) -> pd.DataFrame:
    """Synthetic aligned frame where ``hospital_demand`` is a noisy Poisson
    function of ``driver`` shifted ``lag`` weeks earlier — a known relationship
    the regression should be able to recover, mirroring how make_samples plants
    a recoverable lead for the correlation tests."""
    rng = np.random.default_rng(seed)
    weeks = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    driver = pd.Series(np.sin(np.arange(n) * 2 * np.pi / 12), index=weeks)
    rate = np.exp(2.5 + beta * driver.shift(lag).fillna(0.0))
    demand = pd.Series(rng.poisson(rate), index=weeks, dtype=float)
    return pd.DataFrame({"driver": driver, "hospital_demand": demand})


def test_build_lagged_design_matrix_shifts_and_drops_na():
    aligned = pd.DataFrame({
        "driver": [1.0, 2.0, 3.0, 4.0, 5.0],
        "hospital_demand": [10.0, 20.0, 30.0, 40.0, 50.0],
    })
    design = build_lagged_design_matrix(aligned, "hospital_demand", {"driver": 2})
    # driver.shift(2) drops the first two rows; "driver" at the first remaining
    # row should be the *original* row-0 value (3 weeks back paired with row 2).
    assert list(design.index) == [2, 3, 4]
    assert design["driver"].tolist() == [1.0, 2.0, 3.0]
    assert design["hospital_demand"].tolist() == [30.0, 40.0, 50.0]


def test_build_lagged_design_matrix_validates_columns():
    aligned = pd.DataFrame({"driver": [1.0, 2.0], "hospital_demand": [1.0, 2.0]})
    with pytest.raises(ValueError, match="not in aligned columns"):
        build_lagged_design_matrix(aligned, "missing_response", {"driver": 1})
    with pytest.raises(ValueError, match="Driver"):
        build_lagged_design_matrix(aligned, "hospital_demand", {"missing_driver": 1})


def test_fit_count_regression_recovers_planted_lagged_relationship():
    """A driver planted with a positive effect at lag 2 should come out
    positive and statistically significant when given the correct lag."""
    aligned = _planted_aligned(n=80, lag=2, beta=0.8)
    result = fit_count_regression(aligned, "hospital_demand", {"driver": 2})

    assert result.family == "poisson"
    assert result.n_obs == len(aligned) - 2
    assert result.coefficients["driver"] > 0
    assert result.pvalues["driver"] < 0.05
    assert result.pseudo_r_squared > 0
    assert len(result.fitted) == result.n_obs


def test_fit_count_regression_rejects_unknown_family():
    aligned = _planted_aligned()
    with pytest.raises(ValueError, match="Unknown family"):
        fit_count_regression(aligned, "hospital_demand", {"driver": 2}, family="logit")


def test_fit_count_regression_requires_enough_overlap():
    aligned = _planted_aligned(n=10)
    with pytest.raises(ValueError, match="Only .* overlapping weeks"):
        fit_count_regression(aligned, "hospital_demand", {"driver": 8})
