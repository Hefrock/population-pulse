"""Accumulating merge for timeseries signals (``transit``, ``weather``).

Unlike ``events`` (an *upcoming-events* snapshot that needs a separate
``events_archive.parquet`` -- see ``events_archive.py``), the ``transit`` and
``weather`` fetchers already return real historical data for whatever
``start``/``end`` window ``run.py`` asks for. The only problem is that each
run *overwrites* ``transit.parquet`` / ``weather.parquet`` with that window,
so the file never holds more than ~365 days even though the underlying APIs
(MBTA gated entries back to 2014, Open-Meteo's archive back decades) can
supply much more.

This module merges a fresh fetch into the existing same-named parquet file
in place -- no new files, no dashboard changes -- so a wide one-time
``workflow_dispatch`` backfill plus the daily rolling fetch accumulate
permanently instead of being overwritten back down to the rolling window.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import requests

REQUEST_TIMEOUT = 20


def load_existing(local_path: Path, archive_url: str, columns: list[str]) -> pd.DataFrame:
    """Load whatever has accumulated so far: local file first, then the data branch.

    Returns an empty (correctly-typed) frame if neither is available -- the
    first run anywhere starts the accumulation from scratch.
    """
    if local_path.exists():
        return pd.read_parquet(local_path)

    try:
        resp = requests.get(archive_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return pd.read_parquet(io.BytesIO(resp.content))
    except Exception:
        return pd.DataFrame(columns=columns)


def merge(existing: pd.DataFrame, new: pd.DataFrame, key_columns: list[str]) -> pd.DataFrame:
    """Fold a fresh fetch into the accumulated history.

    Rows are deduplicated on ``key_columns`` (e.g. ``["timestamp", "route"]``
    for transit, ``["timestamp"]`` for weather's wide form), keeping the
    *newer* fetch's row when both cover the same key -- so a re-fetched
    window refreshes values without duplicating rows. Rows in ``existing``
    outside the new fetch's window are kept as-is, which is how the
    accumulation grows over time. Sorted by timestamp.
    """
    if existing.empty:
        combined = new.copy()
    else:
        combined = pd.concat([existing, new], ignore_index=True)

    if combined.empty:
        return combined

    combined = combined.drop_duplicates(subset=key_columns, keep="last")
    return combined.sort_values("timestamp").reset_index(drop=True)
