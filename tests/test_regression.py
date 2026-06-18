"""Tests for the multi-driver lagged count regression."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.regression import (
    build_lagged_design_matrix,
    build_surge_labels,
    driver_vif,
    fit_count_regression,
    fit_logistic_regression,
)


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


def test_driver_vif_flags_a_collinear_driver():
    """A driver that's almost a copy of another should get a high VIF; an
    independent third driver should stay low."""
    rng = np.random.default_rng(3)
    n = 60
    weeks = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    base = rng.standard_normal(n)
    aligned = pd.DataFrame({
        "a": base,
        "b": base + rng.normal(scale=0.01, size=n),
        "c": rng.standard_normal(n),
        "hospital_demand": rng.standard_normal(n),
    }, index=weeks)

    vif_df = driver_vif(aligned, deseasonalize=False)
    vif = vif_df.set_index("driver")["vif"]
    assert vif["a"] > 10
    assert vif["b"] > 10
    assert vif["c"] < 5


def test_driver_vif_returns_empty_with_insufficient_overlap():
    aligned = pd.DataFrame({
        "a": [1.0, 2.0, 3.0],
        "b": [2.0, 4.0, 6.0],
        "hospital_demand": [1.0, 2.0, 3.0],
    })
    assert driver_vif(aligned, min_obs=20).empty


def _planted_surge_aligned(n: int = 120, lag: int = 2, seed: int = 1) -> pd.DataFrame:
    """Synthetic aligned frame with a seasonal ``hospital_demand`` plus
    periodic "surge" spikes, and a ``driver`` that spikes ``lag`` weeks before
    each surge — a known relationship ``fit_logistic_regression`` should be
    able to recover."""
    rng = np.random.default_rng(seed)
    weeks = pd.date_range("2022-01-02", periods=n, freq="W", tz="UTC")
    seasonal = 100 + 30 * np.sin(np.arange(n) * 2 * np.pi / 52)

    surge_idx = np.arange(5, n, 8)
    spike = np.zeros(n)
    spike[surge_idx] = 50
    demand = pd.Series(seasonal + spike + rng.normal(0, 3, n), index=weeks)

    driver_idx = surge_idx[surge_idx - lag >= 0] - lag
    driver = np.zeros(n)
    driver[driver_idx] = 10
    driver = pd.Series(driver + rng.normal(0, 1, n), index=weeks)

    return pd.DataFrame({"driver": driver, "hospital_demand": demand})


def test_build_surge_labels_is_binary_and_relative_to_season():
    """A purely seasonal series (no extra surges) shouldn't have its "surge"
    weeks all fall in the high-level season — the label is relative to a
    rolling baseline, not the raw level."""
    n = 104
    weeks = pd.date_range("2024-01-07", periods=n, freq="W", tz="UTC")
    seasonal = pd.Series(100 + 50 * np.sin(np.arange(n) * 2 * np.pi / 52), index=weeks)

    labels = build_surge_labels(seasonal, quantile=0.75)

    assert set(labels.unique()) <= {0, 1}
    assert 0 < labels.sum() < n
    # Roughly a quarter of weeks should be flagged for quantile=0.75.
    assert labels.sum() == pytest.approx(n * 0.25, abs=n * 0.1)


def test_fit_logistic_regression_recovers_planted_lagged_predictor():
    """A driver planted 2 weeks ahead of each surge should come out positive
    and statistically significant, with above-chance AUC."""
    aligned = _planted_surge_aligned(n=120, lag=2)
    result = fit_logistic_regression(aligned, "hospital_demand", {"driver": 2})

    assert result.n_obs == len(aligned) - 2
    assert result.coefficients["driver"] > 0
    assert result.pvalues["driver"] < 0.05
    assert result.auc > 0.7
    assert 0 < result.pseudo_r_squared <= 1
    assert 0 < result.n_surge_weeks < result.n_obs
    assert len(result.fitted_probabilities) == result.n_obs
    assert len(result.surge_labels) == result.n_obs


def test_fit_logistic_regression_requires_enough_overlap():
    aligned = _planted_surge_aligned(n=10)
    with pytest.raises(ValueError, match="Only .* overlapping weeks"):
        fit_logistic_regression(aligned, "hospital_demand", {"driver": 8})


def test_fit_logistic_regression_rejects_single_class_label():
    aligned = _planted_surge_aligned(n=120, lag=2)
    with pytest.raises(ValueError, match="only one class"):
        # quantile=1.0 -> threshold == max residual -> nothing is strictly
        # greater -> every week labeled 0.
        fit_logistic_regression(aligned, "hospital_demand", {"driver": 2}, surge_quantile=1.0)
