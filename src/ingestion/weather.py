"""Open-Meteo weather fetcher.

Open-Meteo needs no API key, so this is a fully working fetcher. It pulls the
historical hourly series for the configured variables (temperature, apparent
temperature, precipitation) — the drivers behind heat-stress, cold-related, and
asthma ED visits in the weather sub-hypothesis.

Uses the archive endpoint for past dates; for very recent dates Open-Meteo
serves them through the forecast endpoint's ``past_days`` parameter. Phase 1
keeps it simple and uses the historical archive API.

Two-tier fallback, mirroring the rest of the pipeline: if Open-Meteo is
unreachable or errors, fall back to the bundled synthetic sample rather than
silently dropping the signal for that run.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
REQUEST_TIMEOUT = 30
SAMPLE_PATH = Path("data/samples/weather_sample.csv")


def fetch_open_meteo(
    base_url: str,
    latitude: float,
    longitude: float,
    variables: list[str],
    start: str,
    end: str,
    timezone: str,
) -> pd.DataFrame:
    """Return hourly weather as ``timestamp`` + one column per variable.

    ``base_url`` from config is accepted for interface symmetry but the archive
    endpoint is used for historical ranges. Falls back to the bundled sample if
    Open-Meteo is unreachable or returns an error.
    """
    try:
        resp = requests.get(
            ARCHIVE_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "start_date": start,
                "end_date": end,
                "hourly": ",".join(variables),
                "timezone": timezone,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        hourly = resp.json().get("hourly", {})
    except Exception as exc:  # noqa: BLE001 — degrade to the sample tier
        print(
            "\n⚠️  WARNING: [weather] Open-Meteo unavailable "
            f"({exc}); falling back to SYNTHETIC sample data.\n"
            "   Correlations computed with this data are not meaningful.\n"
        )
        return _load_sample(variables, start, end, timezone)

    if not hourly:
        return pd.DataFrame(columns=["timestamp", *variables])

    df = pd.DataFrame(hourly).rename(columns={"time": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(
        timezone, nonexistent="shift_forward", ambiguous=False
    )
    return df


def _load_sample(variables: list[str], start: str, end: str, timezone: str) -> pd.DataFrame:
    """Bundled hourly sample with planted seasonal extremes, clipped to [start, end].

    The committed sample spans a fixed ~1-year window anchored to whenever
    ``make_samples`` last ran, but ``run.py`` (and the tests) request a window
    relative to *today*. Once the sample ages far enough that [start, end] no
    longer overlaps it, a naive clip returns zero rows — silently dropping the
    signal on any run that falls back here, which defeats the point of the
    sample tier. To stay fail-soft, shift the sample by whole years until it
    covers the requested window before clipping. The planted signal is
    day-of-year seasonal, so a whole-year shift preserves it; shifting the
    naive timestamps *before* localizing keeps the DST guards (below) in charge
    of the spring-forward/fall-back hours rather than tripping over them.
    """
    if not SAMPLE_PATH.exists():
        print(f"[weather] No sample at {SAMPLE_PATH}; continuing with no signal.")
        return pd.DataFrame(columns=["timestamp", *variables])

    df = pd.read_csv(SAMPLE_PATH, parse_dates=["timestamp"])

    # Naive year-shift so an aged sample still covers a today-relative window:
    # slide it by whole years until it overlaps [start, end]. Bounded by the
    # gap between the sample and the window, so it terminates.
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if not df.empty:
        year = pd.DateOffset(years=1)
        while df["timestamp"].max() < start_ts:  # sample ends before the window
            df["timestamp"] = df["timestamp"] + year
        while df["timestamp"].min() > end_ts:    # sample starts after the window
            df["timestamp"] = df["timestamp"] - year

    df["timestamp"] = df["timestamp"].dt.tz_localize(
        timezone, nonexistent="shift_forward", ambiguous=False
    )
    mask = (df["timestamp"] >= pd.Timestamp(start, tz=timezone)) & (
        df["timestamp"] <= pd.Timestamp(end, tz=timezone)
    )
    available = [v for v in variables if v in df.columns]
    return df.loc[mask, ["timestamp", *available]].reset_index(drop=True)
