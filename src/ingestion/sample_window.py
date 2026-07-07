"""Shared helper for the synthetic-sample fallback tiers.

The bundled samples (``data/samples/*.csv``) span a fixed ~1-year window frozen
at ``make_samples`` generation time, but ``run.py`` requests a window relative
to *today*. Once a committed sample ages past that window, a naive clip to
``[start, end]`` returns zero rows — silently dropping the signal on any run
that falls back to the sample tier. That is exactly what broke daily ingestion
via the weather fetcher; see ``weather.py``.

``shift_sample_to_window`` slides the sample by whole years until it overlaps
the requested window, so the fallback stays genuinely fail-soft. The planted
signal is day-of-year seasonal, so a whole-year shift preserves it. It operates
on **naive** timestamps so each caller's own tz-localization (with its DST
guards) still runs afterward — call this *before* localizing.

Only the synthetic sample tiers use this. Real-source parsers must keep
clipping out-of-window data, not shifting it into range.
"""

from __future__ import annotations

import pandas as pd


def shift_sample_to_window(
    df: pd.DataFrame, start: str, end: str, timestamp_col: str = "timestamp"
) -> pd.DataFrame:
    """Slide ``timestamp_col`` by whole years until it overlaps ``[start, end]``.

    Returns a copy with the shift applied; a no-op (returning ``df`` unchanged)
    if the frame is empty or already overlaps the window. ``start``/``end`` are
    parsed as naive timestamps, matching the naive sample timestamps.
    """
    if df.empty:
        return df
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    year = pd.DateOffset(years=1)
    out = df.copy()
    # Slide forward if the sample ends before the window, back if it starts
    # after. The two conditions are mutually exclusive, and each step moves a
    # whole year toward the window, so this terminates.
    while out[timestamp_col].max() < start_ts:
        out[timestamp_col] = out[timestamp_col] + year
    while out[timestamp_col].min() > end_ts:
        out[timestamp_col] = out[timestamp_col] - year
    return out
