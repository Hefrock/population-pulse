"""Tests for the multiple-comparisons correction helper."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.multiple_comparisons import apply_correction


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
