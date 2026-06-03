"""Ticketmaster Discovery API fetcher for large events.

Pulls Sports and Music events in Boston using the Discovery API v2.
These segments drive measurable population-flow spikes (TD Garden,
Fenway Park, Gillette Stadium).

Key constraints:
  - The API only returns *upcoming* events; past events are purged.
  - The fetcher always queries from today to today+365 regardless of
    the pipeline date window, because backward-looking queries return
    nothing useful.
  - ``classificationName`` (not ``segmentName``) is the correct filter
    for multi-category queries; it accepts a comma-separated list.

Get a free key at https://developer.ticketmaster.com/ and set
TICKETMASTER_API_KEY in your environment / .env file.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pandas as pd
import requests

REQUEST_TIMEOUT = 30
PAGE_SIZE = 200  # max allowed by Discovery API
FORWARD_DAYS = 365  # always fetch this far ahead


def fetch_events(
    base_url: str,
    city: str,
    state_code: str,
    start: str,
    end: str,
    timezone: str,
    segments: list[str] | None = None,
) -> pd.DataFrame:
    """Return upcoming events from Ticketmaster.

    The ``start`` / ``end`` window from the pipeline is ignored in favour of
    today → today+FORWARD_DAYS, because the Discovery API only exposes
    future events. This is logged so it is never silent.

    Returns columns: timestamp, venue, name, expected_attendance, source.
    """
    api_key = os.environ.get("TICKETMASTER_API_KEY")
    if not api_key:
        print("[ticketmaster] No TICKETMASTER_API_KEY set; skipping.")
        return _empty_frame()

    today = date.today()
    q_start = today.isoformat()
    q_end = (today + timedelta(days=FORWARD_DAYS)).isoformat()
    print(
        f"[ticketmaster] Querying upcoming events {q_start} → {q_end} "
        f"(API only exposes future events; pipeline window {start}→{end} ignored)."
    )

    params: dict = {
        "apikey": api_key,
        "city": city,
        "stateCode": state_code,
        "startDateTime": f"{q_start}T00:00:00Z",
        "endDateTime": f"{q_end}T23:59:59Z",
        "size": PAGE_SIZE,
        "sort": "date,asc",
    }
    # classificationName accepts a comma-separated list and is the correct
    # parameter for multi-category queries. segmentName only accepts one value.
    if segments:
        params["classificationName"] = ",".join(segments)

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
                "expected_attendance": None,
                "source": "ticketmaster",
            })

        page_info = data.get("page", {})
        if page >= page_info.get("totalPages", 1) - 1:
            break
        page += 1

    if not rows:
        print("[ticketmaster] No events returned.")
        return _empty_frame()

    df = pd.DataFrame(rows).reset_index(drop=True)
    print(f"[ticketmaster] {len(df)} upcoming events fetched.")
    return df


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["timestamp", "venue", "name", "expected_attendance", "source"]
    )
