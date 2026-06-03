"""MBTA V3 API fetcher — the reference 'real' fetcher for Phase 1.

The MBTA V3 API (https://api-v3.mbta.com) exposes real-time vehicle positions,
predictions, and alerts. It does *not* directly expose historical turnstile
entry/exit counts through the live JSON API — those come from the MBTA's
separate open-data / performance downloads. So this module does two things:

1. ``fetch_ridership`` — the interface method. In Phase 1 it loads a bundled
   sample so the pipeline and dashboard run end-to-end without a key. Set
   ``MBTA_API_KEY`` and pass ``live=True`` to hit the API.
2. ``fetch_live_vehicle_counts`` — a working live call that counts active
   vehicles per route right now, as a simple real-time flow proxy. This is a
   genuine API call you can run today with a free key.

Get a free key at https://api-v3.mbta.com/ and put it in your .env as
MBTA_API_KEY.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import requests

SAMPLE_PATH = Path("data/samples/mbta_ridership_sample.csv")
REQUEST_TIMEOUT = 30


def fetch_ridership(
    base_url: str,
    routes: list[str],
    start: str,
    end: str,
    timezone: str,
    live: bool = False,
) -> pd.DataFrame:
    """Return transit flow as ``timestamp``, ``route``, ``value``.

    In sample mode (default) this reads a bundled CSV so the rest of the
    pipeline is exercisable offline. In live mode it falls back to a snapshot of
    current vehicle counts (see ``fetch_live_vehicle_counts``), which is the
    real-time proxy available without historical-data downloads.
    """
    if not live:
        return _load_sample(start, end, timezone)

    # Live mode: snapshot current vehicle activity per route. This is a true
    # call against the public API. It's a "right now" measure, so it's most
    # useful when run on a schedule (see .github/workflows/) to build a
    # time series over days.
    snapshot = fetch_live_vehicle_counts(base_url, routes)
    return snapshot


def fetch_live_vehicle_counts(base_url: str, routes: list[str]) -> pd.DataFrame:
    """Count in-service vehicles per route right now via the V3 API.

    Returns one row per route with the current timestamp. A working example of
    hitting the real endpoint; expand later to use ridership/performance data.
    """
    api_key = os.environ.get("MBTA_API_KEY")
    headers = {"x-api-key": api_key} if api_key else {}
    if not api_key:
        # The API works key-less for light use but rate-limits hard. Warn, don't
        # fail — this keeps first-run friction low.
        print("[mbta] No MBTA_API_KEY set; using unauthenticated (rate-limited) access.")

    now = pd.Timestamp.now(tz="UTC")
    rows = []
    for route in routes:
        resp = requests.get(
            f"{base_url}/vehicles",
            params={"filter[route]": route},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        count = len(resp.json().get("data", []))
        rows.append({"timestamp": now, "route": route, "value": count})

    return pd.DataFrame(rows)


def _load_sample(start: str, end: str, timezone: str) -> pd.DataFrame:
    """Load and window the bundled sample ridership series."""
    if not SAMPLE_PATH.exists():
        raise FileNotFoundError(
            f"Sample data missing at {SAMPLE_PATH}. "
            "Run `python -m src.ingestion.make_samples` to regenerate it."
        )
    df = pd.read_csv(SAMPLE_PATH, parse_dates=["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(timezone)
    mask = (df["timestamp"] >= pd.Timestamp(start, tz=timezone)) & (
        df["timestamp"] <= pd.Timestamp(end, tz=timezone)
    )
    return df.loc[mask].reset_index(drop=True)
