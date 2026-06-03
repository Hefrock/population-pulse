"""Ticketmaster Discovery API fetcher for large events.

Pulls Sports and Music events in Boston by date range using the Discovery
API v2. These segments drive measurable population-flow spikes (TD Garden,
Fenway Park, Gillette Stadium).

Get a free key at https://developer.ticketmaster.com/ and set
TICKETMASTER_API_KEY in your environment / .env file.
"""

from __future__ import annotations

import os

import pandas as pd
import requests

REQUEST_TIMEOUT = 30
PAGE_SIZE = 200  # max allowed by Discovery API


def fetch_events(
    base_url: str,
    city: str,
    state_code: str,
    start: str,
    end: str,
    timezone: str,
    segments: list[str] | None = None,
) -> pd.DataFrame:
    """Return events from Ticketmaster between start and end.

    Returns columns: timestamp, venue, name, expected_attendance, source.
    Capacity is rarely available in the free tier; expected_attendance will
    be None for most rows — the event date and venue are the useful signal.
    """
    api_key = os.environ.get("TICKETMASTER_API_KEY")
    if not api_key:
        print("[ticketmaster] No TICKETMASTER_API_KEY set; skipping.")
        return _empty_frame()

    params: dict = {
        "apikey": api_key,
        "city": city,
        "stateCode": state_code,
        "startDateTime": f"{start}T00:00:00Z",
        "endDateTime": f"{end}T23:59:59Z",
        "size": PAGE_SIZE,
        "sort": "date,asc",
    }
    if segments:
        params["segmentName"] = ",".join(segments)

    rows = []
    page = 0

    while True:
        resp = requests.get(
            f"{base_url}/events.json",
            params={**params, "page": page},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        for event in data.get("_embedded", {}).get("events", []):
            date_str = event.get("dates", {}).get("start", {}).get("localDate")
            if not date_str:
                continue

            venue_info = event.get("_embedded", {}).get("venues", [{}])[0]
            rows.append({
                "timestamp": pd.Timestamp(date_str, tz=timezone),
                "venue": venue_info.get("name", "Unknown"),
                "name": event.get("name", "Unknown"),
                "expected_attendance": None,  # not exposed in free tier
                "source": "ticketmaster",
            })

        page_info = data.get("page", {})
        if page >= page_info.get("totalPages", 1) - 1:
            break
        page += 1

    if not rows:
        print("[ticketmaster] No events returned for this date range.")
        return _empty_frame()

    df = pd.DataFrame(rows).reset_index(drop=True)
    print(f"[ticketmaster] {len(df)} events fetched.")
    return df


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["timestamp", "venue", "name", "expected_attendance", "source"]
    )
