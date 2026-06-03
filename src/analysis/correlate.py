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

from dataclasses import dataclass

import numpy as np
import pandas as pd


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


@dataclass
class CrossCorrResult:
    """Result of a lagged cross-correlation scan."""

    lags: list[int]
    correlations: list[float]
    best_lag: int
    best_corr: float
    deseasonalized: bool


def lagged_cross_correlation(
    driver: pd.Series,
    response: pd.Series,
    max_lag: int = 8,
    deseasonalize: bool = True,
) -> CrossCorrResult:
    """Correlate ``driver`` against ``response`` at lags 0..``max_lag``.

    A positive lag means the driver *leads* the response (driver at time t vs
    response at t+lag) — the direction we expect if population surges precede
    hospital demand.

    If ``deseasonalize`` is set, both series have a rolling seasonal mean
    removed first, which guards against the "everything trends together in
    winter" trap. This is on by default precisely because the naive version is
    so easy to misread.
    """
    df = pd.concat({"driver": driver, "response": response}, axis=1).dropna()
    if len(df) < max_lag + 3:
        raise ValueError("Not enough overlapping data points for this lag range.")

    d = df["driver"].astype(float)
    r = df["response"].astype(float)

    if deseasonalize:
        # Remove a centered rolling mean as a cheap seasonal/trend filter.
        window = min(13, max(3, len(df) // 4))
        d = d - d.rolling(window, center=True, min_periods=1).mean()
        r = r - r.rolling(window, center=True, min_periods=1).mean()

    lags = list(range(0, max_lag + 1))
    corrs = []
    for lag in lags:
        if lag == 0:
            corrs.append(float(d.corr(r)))
        else:
            # Strip index before correlating: pandas .corr() aligns by index,
            # so d.iloc[:-lag] and r.iloc[lag:] (different index ranges) would
            # intersect and compare the same timestamps rather than the intended
            # positional shift. Using .values avoids this.
            corrs.append(float(
                pd.Series(d.values[:-lag]).corr(pd.Series(r.values[lag:]))
            ))

    corrs_clean = [c if not np.isnan(c) else 0.0 for c in corrs]
    best_idx = int(np.argmax(np.abs(corrs_clean)))
    return CrossCorrResult(
        lags=lags,
        correlations=corrs_clean,
        best_lag=lags[best_idx],
        best_corr=corrs_clean[best_idx],
        deseasonalized=deseasonalize,
    )
