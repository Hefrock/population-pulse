"""CDC FluView ILINet fetcher via Delphi Epidata API.

The Delphi Epidata API (CMU) mirrors CDC FluView ILINet data and provides
a clean JSON endpoint — no API key, no extra package, goes back to 1997.

ILI counts are a proxy for ED demand (sentinel physician offices, not EDs),
but they are real clinical data and track respiratory ED visits closely.
The MA DPH manual CSV will take precedence when present.

Reference: https://api.delphi.cmu.edu/epidata/
"""

from __future__ import annotations

import datetime

import pandas as pd
import requests

EPIDATA_BASE = "https://api.delphi.cmu.edu/epidata"
REQUEST_TIMEOUT = 30

# State name → 2-letter Epidata region code
_STATE_CODES: dict[str, str] = {
    "massachusetts": "ma",
    "california": "ca",
    "new york": "ny",
    "texas": "tx",
    "florida": "fl",
}


def fetch_ili_data(
    state: str,
    start: str,
    end: str,
    timezone: str,
) -> pd.DataFrame:
    """Return weekly ILI patient counts from CDC FluView via Delphi Epidata.

    Returns columns: timestamp, metric, value.
    Metrics: ``ili_patients`` and ``total_patients``.
    """
    region = _STATE_CODES.get(state.lower())
    if not region:
        print(f"[cdc_fluview] No region code for '{state}'; add it to _STATE_CODES.")
        return _empty_frame()

    start_ew = _date_to_epiweek(datetime.date.fromisoformat(start))
    end_ew = _date_to_epiweek(datetime.date.fromisoformat(end))

    try:
        resp = requests.get(
            f"{EPIDATA_BASE}/fluview/",
            params={"regions": region, "epiweeks": f"{start_ew}-{end_ew}"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[cdc_fluview] Request failed: {exc}")
        return _empty_frame()

    if data.get("result") != 1 or not data.get("epidata"):
        print(f"[cdc_fluview] No ILI data returned for {state}.")
        return _empty_frame()

    rows = []
    for record in data["epidata"]:
        ts = _epiweek_to_timestamp(record["epiweek"], timezone)
        if pd.notna(record.get("num_ili")):
            rows.append({"timestamp": ts, "metric": "ili_patients", "value": float(record["num_ili"])})
        if pd.notna(record.get("num_patients")):
            rows.append({"timestamp": ts, "metric": "total_patients", "value": float(record["num_patients"])})

    if not rows:
        print(f"[cdc_fluview] No usable records in range for {state}.")
        return _empty_frame()

    df = pd.DataFrame(rows).reset_index(drop=True)
    print(f"[cdc_fluview] {len(df)} rows fetched for {state}.")
    return df


def _date_to_epiweek(d: datetime.date) -> int:
    """Convert a date to MMWR epiweek in YYYYWW format (ISO week approximation)."""
    iso_year, iso_week, _ = d.isocalendar()
    return iso_year * 100 + iso_week


def _epiweek_to_timestamp(epiweek: int, timezone: str) -> pd.Timestamp:
    """Convert YYYYWW epiweek to a timezone-aware Sunday timestamp.

    Uses ISO week as an approximation of MMWR week. They share the same week-1
    anchor (week containing Jan 4) but differ in start day (ISO=Monday,
    MMWR=Sunday). For weekly correlation this is a ≤1-day offset, which is
    acceptable at weekly resolution.
    """
    year, week = divmod(epiweek, 100)
    dt = datetime.datetime.strptime(f"{year}-W{week:02d}-0", "%G-W%V-%w")
    return pd.Timestamp(dt).tz_localize(
        timezone, nonexistent="shift_forward", ambiguous="raise"
    )


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["timestamp", "metric", "value"])
