"""Multiple-comparisons correction for the driver x lag x model scans this
project runs everywhere (README's "What we've found so far", the dashboard's
Correlation & Regression tab). Every p-value reported so far has been
interpreted in isolation, with no accounting for how many tests were run to
find it -- the difference between "the one driver that survived" and "the one
driver that happened to survive the most looks" is exactly what a correction
makes visible.

Benjamini-Hochberg (false discovery rate control) is the default here rather
than Bonferroni: the tests this project runs are not independent (adjacent
lags of the same driver are highly correlated with each other, and several
drivers are correlated with each other too -- see
``correlate.driver_correlation_matrix``). Bonferroni's guarantee assumes
independence and becomes needlessly conservative for correlated tests,
throwing away real signal. BH is the standard choice for exploratory
multi-hypothesis screens in genomics/epidemiology-style analysis for the
same reason.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from statsmodels.stats.multitest import multipletests

from src.analysis.correlate import CrossCorrResult


@dataclass
class CorrectionResult:
    """Multiple-comparisons-corrected view of a batch of p-values."""

    labels: list
    pvalues: list[float]
    corrected_pvalues: list[float]
    significant: list[bool]
    method: str
    alpha: float
    n_tests: int
    n_significant_raw: int
    n_significant_corrected: int


def apply_correction(
    pvalues: dict | pd.Series,
    alpha: float = 0.05,
    method: str = "fdr_bh",
) -> CorrectionResult:
    """Apply a multiple-comparisons correction across a batch of p-values.

    ``pvalues`` maps a label (e.g. a lag, or a "driver @ lag" string) to its
    raw p-value. The caller defines what counts as *the family* being
    corrected together, and that choice matters: correcting across one
    driver's lags tested by ``lagged_cross_correlation`` is a narrower (more
    lenient) family than correcting across every driver x lag combination
    ever reported anywhere. Be explicit about which family a given
    correction actually covers when reporting results.

    ``method`` is any method name accepted by
    ``statsmodels.stats.multitest.multipletests`` -- defaults to
    ``"fdr_bh"`` (Benjamini-Hochberg; see module docstring for why). Pass
    ``"bonferroni"`` for the stricter, independence-assuming alternative.
    """
    if isinstance(pvalues, pd.Series):
        labels = pvalues.index.tolist()
        raw = pvalues.to_numpy(dtype=float)
    else:
        labels = list(pvalues.keys())
        raw = np.array(list(pvalues.values()), dtype=float)

    if len(raw) == 0:
        raise ValueError("Need at least one p-value to correct.")

    reject, corrected, _, _ = multipletests(raw, alpha=alpha, method=method)

    return CorrectionResult(
        labels=labels,
        pvalues=raw.tolist(),
        corrected_pvalues=corrected.tolist(),
        significant=reject.tolist(),
        method=method,
        alpha=alpha,
        n_tests=len(raw),
        n_significant_raw=int((raw < alpha).sum()),
        n_significant_corrected=int(reject.sum()),
    )


def summarize_scan(
    results: dict[str, CrossCorrResult],
    alpha: float = 0.05,
    method: str = "fdr_bh",
) -> tuple[pd.DataFrame, CorrectionResult]:
    """Correct a whole ``correlate.scan_drivers()`` output together.

    Unlike correcting one driver's own lags in isolation, this treats *every*
    (driver, lag) p-value tested across the whole scan as one family -- the
    "the one driver that survived, or the one that survived the most looks
    across everything ever tested?" question at full scope, not just within
    one driver's own 9-lag sweep.

    Returns a one-row-per-driver summary (that driver's own best lag, with
    the corrected p-value from the *full* scan-wide correction) sorted by
    corrected p-value, plus the underlying ``CorrectionResult`` covering
    every individual (driver, lag) pair for anyone who wants that detail.
    """
    flat = {
        f"{driver}@{lag}": p
        for driver, result in results.items()
        for lag, p in zip(result.lags, result.pvalues)
    }
    correction = apply_correction(flat, alpha=alpha, method=method)
    corrected_by_label = dict(zip(correction.labels, correction.corrected_pvalues))
    significant_by_label = dict(zip(correction.labels, correction.significant))

    rows = []
    for driver, result in results.items():
        label = f"{driver}@{result.best_lag}"
        rows.append({
            "driver": driver,
            "best_lag": result.best_lag,
            "correlation": result.best_corr,
            "raw_pvalue": result.best_pvalue,
            "corrected_pvalue": corrected_by_label[label],
            "significant": significant_by_label[label],
            "ambiguous": result.ambiguous,
        })
    summary_df = pd.DataFrame(rows).sort_values("corrected_pvalue").reset_index(drop=True)
    return summary_df, correction
