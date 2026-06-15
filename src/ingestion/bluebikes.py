"""Bluebikes (Boston bike-share) fetchers.

Two signals, in priority order:

1. ``fetch_trip_history`` — **historical daily ride counts**. Bluebikes (Lyft)
   publishes monthly trip-level CSVs (zipped) on a public S3 bucket, 2018 to
   present, with no API key. This is the signal we actually want for Phase-1
   correlation: a real, deep-history daily series for the "daily commute"
   sub-hypothesis alongside MBTA ridership. Each row is one ride; we aggregate
   to a daily system-wide count.

2. ``fetch_station_status`` — a real-time snapshot of bikes currently docked
   system-wide via the public GBFS feed. One point per call, so only useful
   when accumulated over many scheduled runs (mirrors MBTA's live
   vehicle-count fallback). Note this is a *stock* measure (bikes sitting in
   docks right now), not the *flow* measure (rides/day) that trip history
   provides — a rough proxy only, used when the trip-history archive is
   unreachable.

``fetch_bikeshare`` is the interface method the provider calls. It prefers
trip history, then the GBFS snapshot, then the bundled sample — always
returning ``timestamp``, ``value`` and never raising for an
empty/unavailable upstream.

CAVEAT on trip history: monthly files follow the
``<YYYYMM>-bluebikes-tripdata.zip`` naming Lyft has used since the 2018
Hubway -> Bluebikes rebrand and are typically published with a ~1-2 month
lag, so the most recent month or two of a request window may come back
empty (the GBFS fallback covers that gap going forward). Pre-2018
"hubway-tripdata" files use a different name and are not fetched.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd
import requests

SAMPLE_PATH = Path("data/samples/bluebikes_sample.csv")
REQUEST_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 240  # monthly trip-data zips can be tens of MB

GBFS_STATION_STATUS_URL = "https://gbfs.bluebikes.com/gbfs/en/station_status.json"

# Column names Lyft has used for a ride's start timestamp across format
# revisions (pre-2021 "Hubway-style" vs. current GBFS-aligned "tripdata").
_START_TIME_COLUMNS = ["started_at", "starttime", "start_time"]


def fetch_bikeshare(
    start: str,
    end: str,
    timezone: str,
    trip_history: dict | None = None,
    gbfs: dict | None = None,
) -> pd.DataFrame:
    """Return daily bikeshare activity as ``timestamp``, ``value``.

    Source priority:
      1. Historical trip-data archive (if ``trip_history`` config is given).
      2. Live GBFS station-status snapshot (if ``gbfs`` config is given).
      3. Bundled sample series.

    Any upstream failure falls through to the next source rather than
    raising, so one flaky endpoint never kills the daily run.
    """
    if trip_history:
        try:
            df = fetch_trip_history(
                base_url=trip_history["base_url"], start=start, end=end, timezone=timezone
            )
            if not df.empty:
                return df
            print("[bluebikes] Trip-history archive returned no rows; falling back.")
        except Exception as exc:  # noqa: BLE001 - fall back, don't kill the run
            print(f"[bluebikes] Trip-history fetch failed ({exc}); falling back.")

    if gbfs:
        try:
            df = fetch_station_status(base_url=gbfs.get("base_url"), timezone=timezone)
            if not df.empty:
                return df
            print("[bluebikes] GBFS station-status snapshot returned no rows; falling back.")
        except Exception as exc:  # noqa: BLE001
            print(f"[bluebikes] GBFS station-status snapshot failed ({exc}); falling back.")

    print("[bluebikes] Using sample data.")
    return _load_sample(start, end, timezone)


# --- Historical trip data ----------------------------------------------------

def fetch_trip_history(base_url: str, start: str, end: str, timezone: str) -> pd.DataFrame:
    """Download monthly trip-data archives covering ``[start, end]`` and
    aggregate ride counts to one row per day.

    Returns ``timestamp`` (daily, tz-aware), ``value`` (ride count). Months
    with no published file (e.g. the current month) are skipped rather than
    treated as an error.
    """
    frames: list[pd.DataFrame] = []
    for year_month in _month_range(start, end):
        url = f"{base_url}/{year_month}-bluebikes-tripdata.zip"
        try:
            resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
            raw = _read_tabular(resp.content)
        except Exception as exc:
            print(f"[bluebikes] {year_month} trip data unavailable ({exc}); skipping.")
            continue

        start_col = _detect_start_column(raw.columns)
        if start_col is None:
            print(f"[bluebikes] No start-time column found in {year_month} trip data "
                  f"(columns={list(raw.columns)}); skipping.")
            continue

        days = pd.to_datetime(raw[start_col], errors="coerce").dt.normalize()
        counts = days.dropna().value_counts().rename_axis("_day").reset_index(name="value")
        frames.append(counts)

    if not frames:
        return pd.DataFrame(columns=["timestamp", "value"])

    combined = pd.concat(frames, ignore_index=True).groupby("_day", as_index=False)["value"].sum()

    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    combined = combined[(combined["_day"] >= lo) & (combined["_day"] <= hi)]
    if combined.empty:
        return pd.DataFrame(columns=["timestamp", "value"])

    out = pd.DataFrame({
        "timestamp": combined["_day"].dt.tz_localize(timezone),
        "value": combined["value"],
    }).sort_values("timestamp").reset_index(drop=True)
    print(f"[bluebikes] {len(out)} daily ride-count rows fetched "
          f"({out['timestamp'].dt.date.min()}..{out['timestamp'].dt.date.max()}).")
    return out


def _month_range(start: str, end: str) -> list[str]:
    """Year-month strings (``YYYYMM``) for every month overlapping ``[start, end]``."""
    months = pd.period_range(pd.Timestamp(start), pd.Timestamp(end), freq="M")
    return [m.strftime("%Y%m") for m in months]


def _read_tabular(content: bytes) -> pd.DataFrame:
    """Read CSV bytes, transparently handling a zipped bundle of CSVs."""
    if content[:2] == b"PK":  # zip magic number
        frames = []
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".csv") and "__macosx" not in name.lower():
                    with zf.open(name) as fh:
                        frames.append(pd.read_csv(fh))
        if not frames:
            raise ValueError("Zip archive contained no CSV files.")
        return pd.concat(frames, ignore_index=True)
    return pd.read_csv(io.BytesIO(content))


def _detect_start_column(columns) -> str | None:
    """Find the ride-start-timestamp column across Lyft's format revisions."""
    lower = {str(c).lower(): c for c in columns}
    for candidate in _START_TIME_COLUMNS:
        if candidate in lower:
            return lower[candidate]
    return None


# --- Live GBFS snapshot (fallback) -------------------------------------------

def fetch_station_status(base_url: str | None, timezone: str) -> pd.DataFrame:
    """Return the system-wide count of currently-docked bikes right now.

    A "right now" stock measure (not rides/day), so it only builds a useful
    series when accumulated over many scheduled runs. Returns one row:
    ``timestamp``, ``value``.
    """
    url = f"{base_url}/en/station_status.json" if base_url else GBFS_STATION_STATUS_URL
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    stations = resp.json().get("data", {}).get("stations", [])
    if not stations:
        return pd.DataFrame(columns=["timestamp", "value"])

    total_bikes = sum(s.get("num_bikes_available", 0) for s in stations)
    now = pd.Timestamp.now(tz="UTC").tz_convert(timezone)
    return pd.DataFrame([{"timestamp": now, "value": total_bikes}])


def _load_sample(start: str, end: str, timezone: str) -> pd.DataFrame:
    """Load and window the bundled sample ride-count series."""
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
