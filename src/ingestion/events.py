"""Events fetcher.

Phase 1 deliberately uses a hand-maintained CSV of known large gatherings. This
gives us clean, precisely-dated events — the cleanest signal for testing the
large-gatherings sub-hypothesis — without scraping fragile venue calendars yet.

The CSV (``data/boston_events.csv``) has columns:
    date, venue, name, expected_attendance

Phase 2 TODO: replace/augment with automated venue-calendar ingestion (TD
Garden, Fenway, BCEC) and possibly a ticketing API.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def fetch_manual_csv(
    path: str,
    start: str,
    end: str,
    timezone: str,
) -> pd.DataFrame:
    """Load events from the manual CSV, windowed to [start, end].

    Returns: ``timestamp``, ``venue``, ``name``, ``expected_attendance``.
    Missing file returns an empty (correctly-typed) frame so the pipeline keeps
    running — events are optional in Phase 1.
    """
    csv_path = Path(path)
    cols = ["timestamp", "venue", "name", "expected_attendance"]
    if not csv_path.exists():
        print(f"[events] No events file at {csv_path}; continuing with no events.")
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(csv_path)
    df = df.rename(columns={"date": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(timezone)

    mask = (df["timestamp"] >= pd.Timestamp(start, tz=timezone)) & (
        df["timestamp"] <= pd.Timestamp(end, tz=timezone)
    )
    out = df.loc[mask].copy()

    for col in cols:
        if col not in out.columns:
            out[col] = pd.NA
    return out[cols].reset_index(drop=True)
