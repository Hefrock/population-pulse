"""Open-Meteo weather fetcher.

Open-Meteo needs no API key, so this is a fully working fetcher. It pulls the
historical hourly series for the configured variables (temperature, apparent
temperature, precipitation) — the drivers behind heat-stress, cold-related, and
asthma ED visits in the weather sub-hypothesis.

Uses the archive endpoint for past dates; for very recent dates Open-Meteo
serves them through the forecast endpoint's ``past_days`` parameter. Phase 1
keeps it simple and uses the historical archive API.
"""

from __future__ import annotations

import pandas as pd
import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
REQUEST_TIMEOUT = 30


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
    endpoint is used for historical ranges.
    """
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

    if not hourly:
        return pd.DataFrame(columns=["timestamp", *variables])

    df = pd.DataFrame(hourly).rename(columns={"time": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(timezone)
    return df
