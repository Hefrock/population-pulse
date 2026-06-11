"""Accumulating archive of events seen across daily ingestion runs.

Ticketmaster and Boston.gov civic events are *upcoming-events* APIs (today ->
today+365 days): each day's fetch is a snapshot of what's currently scheduled,
and ``run.py`` overwrites ``events.parquet`` with that snapshot, discarding
whatever scrolled out of the window. As a result the ``events`` signal has
never had any date overlap with historical ``hospital_demand`` (see README's
"Known limitations").

This module builds a second, accumulating file -- ``events_archive.parquet``
-- by folding each day's snapshot into a running history instead of
overwriting it. It can't backfill the past, but every event seen in a snapshot
stays in the archive once its date arrives, so overlap with ``hospital_demand``
grows by roughly a year for each year the daily pipeline runs.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import requests

REQUEST_TIMEOUT = 20

ARCHIVE_COLUMNS = ["timestamp", "venue", "name", "expected_attendance", "source"]


def load_existing(local_path: Path, archive_url: str) -> pd.DataFrame:
    """Load the archive built so far: local file first, then the data branch.

    Returns an empty (correctly-typed) frame if neither is available -- the
    first run anywhere starts the archive from scratch.
    """
    if local_path.exists():
        return pd.read_parquet(local_path)

    try:
        resp = requests.get(archive_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return pd.read_parquet(io.BytesIO(resp.content))
    except Exception:
        return pd.DataFrame(columns=ARCHIVE_COLUMNS)


def merge(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Fold a fresh snapshot into the archive.

    "Same event" is the same definition ``providers/boston.py`` already uses
    when deduplicating across sources: same calendar date + same event name
    (case-insensitive). When a snapshot re-reports an event already in the
    archive, the newer row wins -- e.g. a later-announced
    ``expected_attendance`` overwrites an earlier null. Sorted by timestamp.
    """
    for col in ARCHIVE_COLUMNS:
        if col not in new.columns:
            new = new.copy()
            new[col] = pd.NA

    combined = pd.concat([existing, new[ARCHIVE_COLUMNS]], ignore_index=True)
    if combined.empty:
        return combined

    combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True)
    combined["_date"] = combined["timestamp"].dt.date.astype(str)
    combined["_name_key"] = combined["name"].astype(str).str.lower().str.strip()
    combined = combined.drop_duplicates(subset=["_date", "_name_key"], keep="last")
    combined = combined.drop(columns=["_date", "_name_key"])
    return combined.sort_values("timestamp").reset_index(drop=True)
