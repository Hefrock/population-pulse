"""Manual events CSV fetcher.

Loads the hand-curated CSV of known large gatherings — clean, precisely-dated
events for the large-gatherings sub-hypothesis. This is one of three event
sources merged in src/providers/boston.py: Ticketmaster (ticketmaster.py) and
Boston.gov civic events (civic_events.py) cover automated venue-calendar and
ticketing data; this manual CSV is the baseline.

The CSV (``data/boston_events.csv``) has columns:
    date, venue, name, expected_attendance
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
