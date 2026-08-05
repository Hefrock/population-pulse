"""Tests for the multiple-comparisons correction helper."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.correlate import CrossCorrResult
from src.analysis.multiple_comparisons import apply_correction, summarize_scan


def _fake_result(lags, correlations, pvalues, best_idx, ambiguous=False):
    return CrossCorrResult(
        lags=lags,
        correlations=correlations,
        pvalues=pvalues,
        ci_low=[float("nan")] * len(lags),
        ci_high=[float("nan")] * len(lags),
        n_obs=[100] * len(lags),
        best_lag=lags[best_idx],
        best_corr=correlations[best_idx],
        best_pvalue=pvalues[best_idx],
        ambiguous=ambiguous,
        deseasonalized=True,
    )


def test_accepts_dict_input():
    result = apply_correction({"a": 0.01, "b": 0.5})
    assert result.labels == ["a", "b"]
    assert result.pvalues == [0.01, 0.5]


def test_accepts_series_input():
    result = apply_correction(pd.Series({"a": 0.01, "b": 0.5}))
    assert result.labels == ["a", "b"]


def test_rejects_empty_input():
    with pytest.raises(ValueError, match="at least one"):
        apply_correction({})


def test_lone_strong_signal_survives_among_many_nulls():
    """One genuinely small p-value among many large (null) ones should
    survive BH correction -- this is the "real signal, not a fluke" case."""
    pvalues = {"real": 0.0001, **{f"null_{i}": 0.6 + i * 0.01 for i in range(20)}}
    result = apply_correction(pvalues, alpha=0.05)
    assert result.significant[result.labels.index("real")] is True
    assert result.n_significant_corrected == 1


def test_borderline_hits_fail_correction_in_a_large_scan():
    """A handful of borderline p-values (nominally < 0.05) scanned alongside
    many clearly-null ones -- e.g. a handful of "hits" out of many driver x
    lag combinations tested -- should mostly not survive correction. This is
    the "the one that survived the most looks" failure mode a correction
    exists to catch."""
    pvalues = {
        "borderline_1": 0.035, "borderline_2": 0.04, "borderline_3": 0.045,
        **{f"null_{i}": v for i, v in enumerate(np.linspace(0.5, 0.9, 17))},
    }
    result = apply_correction(pvalues, alpha=0.05)
    assert result.n_significant_raw == 3
    assert result.n_significant_corrected == 0


def test_corrected_pvalues_are_never_smaller_than_raw():
    """A correction can only make p-values less significant (larger), never
    more -- a basic sanity property of every standard correction method."""
    result = apply_correction({"a": 0.001, "b": 0.02, "c": 0.3, "d": 0.9})
    for raw, corrected in zip(result.pvalues, result.corrected_pvalues):
        assert corrected >= raw


def test_reports_correct_counts_and_metadata():
    result = apply_correction({"a": 0.001, "b": 0.9}, alpha=0.1, method="bonferroni")
    assert result.n_tests == 2
    assert result.method == "bonferroni"
    assert result.alpha == 0.1


def test_summarize_scan_one_row_per_driver():
    results = {
        "a": _fake_result([0, 1], [0.1, 0.2], [0.5, 0.4], best_idx=1),
        "b": _fake_result([0, 1], [0.3, 0.1], [0.2, 0.6], best_idx=0),
    }
    summary_df, correction = summarize_scan(results)
    assert set(summary_df["driver"]) == {"a", "b"}
    assert len(summary_df) == 2
    assert correction.n_tests == 4  # 2 drivers x 2 lags each


def test_summarize_scan_corrects_across_full_family_not_just_own_lags():
    """A driver whose single lag looks borderline in isolation (p=0.04,
    would pass alpha=0.05 alone) should fail once corrected against many
    OTHER drivers' lags too -- the whole point of a scan-wide correction
    over a narrower within-driver-only one."""
    borderline = _fake_result([0], [0.2], [0.04], best_idx=0)
    nulls = {
        f"null_{i}": _fake_result([0], [0.01], [0.7 + i * 0.01], best_idx=0)
        for i in range(20)
    }
    results = {"borderline": borderline, **nulls}
    summary_df, correction = summarize_scan(results, alpha=0.05)
    row = summary_df[summary_df["driver"] == "borderline"].iloc[0]
    assert row["significant"] == False
    assert correction.n_significant_corrected == 0


def test_summarize_scan_strong_signal_survives_and_sorts_first():
    strong = _fake_result([0], [0.5], [0.0001], best_idx=0)
    nulls = {
        f"null_{i}": _fake_result([0], [0.01], [0.7 + i * 0.01], best_idx=0)
        for i in range(20)
    }
    results = {"strong": strong, **nulls}
    summary_df, correction = summarize_scan(results, alpha=0.05)
    row = summary_df[summary_df["driver"] == "strong"].iloc[0]
    assert row["significant"] == True
    assert summary_df.iloc[0]["driver"] == "strong"
