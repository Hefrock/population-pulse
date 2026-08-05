"""Timeline alignment and correlation analysis.

This is where the hypothesis actually gets tested. Two responsibilities:

1. ``align`` — resample every signal onto one common timeline (weekly by
   default, because the hospital data forces that ceiling) and join them into a
   single tidy frame.

2. ``lagged_cross_correlation`` — for a flow/driver signal and the hospital
   signal, compute correlation at a range of lags. This is the right tool
   because effects are expected to be *delayed* (disease incubation, etc.), and
   because looking only at lag-0 correlation would miss them.

IMPORTANT CAVEAT baked into the code: raw correlation between two seasonal
series is misleading — both rise in winter regardless of any causal link. So
``lagged_cross_correlation`` optionally detrends/deseasonalizes first. Treat
results as suggestive, and lean on Phase 2's matched-baseline event studies for
anything stronger.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats


def align(
    signals: dict[str, pd.DataFrame],
    resolution: str = "W",
) -> pd.DataFrame:
    """Resample each signal to ``resolution`` and join on timestamp.

    ``signals`` maps a name to a DataFrame with a ``timestamp`` column and a
    numeric ``value`` column (multi-row signals like transit are summed across
    their sub-categories first). Returns a wide frame indexed by period.
    """
    series = {}
    for name, df in signals.items():
        if df.empty or "timestamp" not in df.columns:
            continue
        s = df.copy()
        s["timestamp"] = pd.to_datetime(s["timestamp"], utc=True)
        value_col = "value" if "value" in s.columns else _first_numeric(s)
        if value_col is None:
            continue
        grouped = (
            s.set_index("timestamp")[value_col]
            .resample(resolution)
            .sum(min_count=1)
        )
        series[name] = grouped

    if not series:
        return pd.DataFrame()
    return pd.concat(series, axis=1)


def _first_numeric(df: pd.DataFrame) -> str | None:
    for col in df.columns:
        if col != "timestamp" and pd.api.types.is_numeric_dtype(df[col]):
            return col
    return None


def seasonal_residual(series: pd.Series, window: int | None = None) -> pd.Series:
    """Remove a centered rolling-mean seasonal/trend component.

    This is the same cheap deseasonalization ``lagged_cross_correlation`` uses
    internally, factored out so other analyses (e.g. the logistic-regression
    "surge" label in ``regression.py``) can define "elevated relative to that
    time of year" the same way correlation does.

    ``window`` defaults to ``min(13, max(3, len(series) // 4))`` — roughly a
    quarter, capped at 13 weeks (one season) and floored at 3.
    """
    s = series.astype(float)
    if window is None:
        window = min(13, max(3, len(s) // 4))
    return s - s.rolling(window, center=True, min_periods=1).mean()


def driver_correlation_matrix(
    aligned: pd.DataFrame,
    drivers: list[str] | None = None,
    deseasonalize: bool = True,
) -> pd.DataFrame:
    """Pairwise correlation between driver columns of an ``align()`` frame.

    Per-driver correlation against the response (``lagged_cross_correlation``)
    says nothing about whether the drivers are correlated with *each other* —
    if two are, a coefficient that looks like an independent effect may
    actually be riding on a correlated peer instead. Deseasonalized by default
    for the same reason as ``lagged_cross_correlation``: two signals that are
    both just "high in winter" will look correlated with no real relationship
    between them.

    ``drivers`` defaults to every column except ``hospital_demand``.
    """
    if drivers is None:
        drivers = [c for c in aligned.columns if c != "hospital_demand"]
    cols = {
        d: seasonal_residual(aligned[d]) if deseasonalize else aligned[d].astype(float)
        for d in drivers
    }
    return pd.DataFrame(cols).corr(min_periods=20)


@dataclass
class CrossCorrResult:
    """Result of a lagged cross-correlation scan."""

    lags: list[int]
    correlations: list[float]
    pvalues: list[float]
    ci_low: list[float]
    ci_high: list[float]
    n_obs: list[int]
    best_lag: int
    best_corr: float
    best_pvalue: float
    ambiguous: bool
    deseasonalized: bool


def _fisher_ci(r: float, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """``1 - alpha`` confidence interval for a Pearson r via the Fisher z-transform.

    Undefined (returns NaN, NaN) for n < 4, where the standard error isn't
    meaningful — genuinely too little data to say anything. ``r`` is clamped
    just inside (-1, 1) before the transform rather than also returning NaN at
    |r| == 1: that's a technicality of the transform's domain, not a sign of
    *more* uncertainty — an exact r=1 is the least ambiguous result possible,
    and reporting "no CI available" there would read backwards.
    """
    if n < 4 or np.isnan(r):
        return (float("nan"), float("nan"))
    r = float(np.clip(r, -1 + 1e-10, 1 - 1e-10))
    z = np.arctanh(r)
    se = 1.0 / np.sqrt(n - 3)
    z_crit = stats.norm.ppf(1 - alpha / 2)
    return (float(np.tanh(z - z_crit * se)), float(np.tanh(z + z_crit * se)))


def _is_ambiguous(ci_low: list[float], ci_high: list[float], order: list[int]) -> bool:
    """True if the runner-up lag's CI overlaps the top-ranked lag's CI.

    ``order`` ranks lag indices by |correlation|, best first. This flags when
    the top pick by ``argmax(|corr|)`` isn't clearly distinguishable from its
    closest competitor — the failure mode that flipped the wastewater headline
    result on a near-tie (lag 0 at +0.44 vs. lag 4 at -0.45; see README).
    """
    if len(order) < 2:
        return False
    best_idx, runner_up_idx = order[0], order[1]
    best_lo, best_hi = ci_low[best_idx], ci_high[best_idx]
    run_lo, run_hi = ci_low[runner_up_idx], ci_high[runner_up_idx]
    if any(np.isnan(v) for v in (best_lo, best_hi, run_lo, run_hi)):
        return True
    return best_lo <= run_hi and run_lo <= best_hi


def lagged_cross_correlation(
    driver: pd.Series,
    response: pd.Series,
    max_lag: int = 8,
    deseasonalize: bool = True,
    alpha: float = 0.05,
) -> CrossCorrResult:
    """Correlate ``driver`` against ``response`` at lags 0..``max_lag``.

    A positive lag means the driver *leads* the response (driver at time t vs
    response at t+lag) — the direction we expect if population surges precede
    hospital demand.

    If ``deseasonalize`` is set, both series have a rolling seasonal mean
    removed first, which guards against the "everything trends together in
    winter" trap. This is on by default precisely because the naive version is
    so easy to misread.

    Each lag also reports a p-value and a ``1 - alpha`` confidence interval
    (Fisher z-transform), and ``ambiguous`` flags when the selected "best" lag
    isn't clearly distinguishable from its closest competitor — a near-tie in
    ``|correlation|`` is not a reliable basis for picking one lag as *the*
    result on its own.
    """
    df = pd.concat({"driver": driver, "response": response}, axis=1).dropna()
    if len(df) < max_lag + 3:
        raise ValueError("Not enough overlapping data points for this lag range.")

    d = df["driver"].astype(float)
    r = df["response"].astype(float)

    if deseasonalize:
        d = seasonal_residual(d)
        r = seasonal_residual(r)

    lags = list(range(0, max_lag + 1))
    corrs, pvalues, n_obs = [], [], []
    for lag in lags:
        # Strip the index before correlating: pandas .corr() aligns by index,
        # so d.iloc[:-lag] and r.iloc[lag:] (different index ranges) would
        # intersect and compare the same timestamps rather than the intended
        # positional shift. Using .values avoids this.
        x = d.values if lag == 0 else d.values[:-lag]
        y = r.values if lag == 0 else r.values[lag:]
        with warnings.catch_warnings():
            # scipy warns (and returns NaN, NaN) on constant input rather than
            # raising — same degenerate case the old .corr()-based NaN handling
            # below already accounted for.
            warnings.simplefilter("ignore")
            corr, pvalue = stats.pearsonr(x, y)
        corrs.append(float(corr))
        pvalues.append(float(pvalue))
        n_obs.append(len(x))

    nan_lags = [lags[i] for i, c in enumerate(corrs) if np.isnan(c)]
    if nan_lags:
        print(
            f"[correlate] NaN at lag(s) {nan_lags} — likely deseasonalization "
            "boundary effect. Treating as 0 for best-lag selection."
        )
    corrs_clean = [c if not np.isnan(c) else 0.0 for c in corrs]
    pvalues_clean = [p if not np.isnan(p) else 1.0 for p in pvalues]
    ci = [_fisher_ci(c, n, alpha) for c, n in zip(corrs_clean, n_obs)]
    ci_low = [lo for lo, _ in ci]
    ci_high = [hi for _, hi in ci]

    order = sorted(range(len(corrs_clean)), key=lambda i: abs(corrs_clean[i]), reverse=True)
    best_idx = order[0]

    return CrossCorrResult(
        lags=lags,
        correlations=corrs_clean,
        pvalues=pvalues_clean,
        ci_low=ci_low,
        ci_high=ci_high,
        n_obs=n_obs,
        best_lag=lags[best_idx],
        best_corr=corrs_clean[best_idx],
        best_pvalue=pvalues_clean[best_idx],
        ambiguous=_is_ambiguous(ci_low, ci_high, order),
        deseasonalized=deseasonalize,
    )


def scan_drivers(
    aligned: pd.DataFrame,
    response: str,
    drivers: list[str] | None = None,
    max_lag: int = 8,
    deseasonalize: bool = True,
) -> dict[str, CrossCorrResult]:
    """Run ``lagged_cross_correlation`` for every driver against ``response``.

    Every result reported anywhere in this project (README, dashboard) has
    tested one driver in isolation. This is the batch version, needed to
    correct across *every* driver x lag combination together rather than
    just one driver's own lags (see ``multiple_comparisons.summarize_scan``,
    which is the natural next step on this function's output) -- the
    difference between "the one driver that survived" and "the one driver
    that happened to survive the most looks *across the whole scan*."

    ``drivers`` defaults to every column of ``aligned`` except ``response``.
    A driver with too little overlapping data to test is silently omitted
    from the result (see ``lagged_cross_correlation``'s ValueError) rather
    than aborting the whole scan -- callers that need to know which drivers
    were skipped should diff ``drivers`` against the returned dict's keys.
    """
    if drivers is None:
        drivers = [c for c in aligned.columns if c != response]
    results = {}
    for driver in drivers:
        try:
            results[driver] = lagged_cross_correlation(
                aligned[driver], aligned[response],
                max_lag=max_lag, deseasonalize=deseasonalize,
            )
        except ValueError:
            continue
    return results
