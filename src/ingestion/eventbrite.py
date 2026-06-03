"""Eventbrite API v3 fetcher for civic and community events.

Covers events that Ticketmaster misses: marathons, festivals, cultural
parades, community health fairs — gatherings that still move population-flow
signals even without a ticketed venue.

Get a Private Token at https://www.eventbrite.com/platform/ and set
EVENTBRITE_API_KEY in your environment / .env file.

Note: Eventbrite's public search API requires an approved key for
broad discovery. The fetcher fails gracefully if the endpoint returns
a 401/403 — events from Ticketmaster and the manual CSV will still load.
"""

from __future__ import annotations

import os

import pandas as pd
import requests

REQUEST_TIMEOUT = 30
PAGE_SIZE = 50  # Eventbrite max per page


def fetch_events(
    base_url: str,
    location: str,
    radius: str,
    start: str,
    end: str,
    timezone: str,
) -> pd.DataFrame:
    """Return public events from Eventbrite between start and end.

    Returns columns: timestamp, venue, name, expected_attendance, source.
    """
    api_key = os.environ.get("EVENTBRITE_API_KEY")
    if not api_key:
        print("[eventbrite] No EVENTBRITE_API_KEY set; skipping.")
        return _empty_frame()

    headers = {"Authorization": f"Bearer {api_key}"}
    params: dict = {
        "location.address": location,
        "location.within": radius,
        "start_date.range_start": f"{start}T00:00:00Z",
        "start_date.range_end": f"{end}T23:59:59Z",
        "expand": "venue",
        "page_size": PAGE_SIZE,
    }

    rows = []
    page = 1

    while True:
        resp = requests.get(
            f"{base_url}/events/search/",
            headers=headers,
            params={**params, "page": page},
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code in (401, 403):
            print(
                "[eventbrite] Access denied — your token may need public-search "
                "approval. Skipping Eventbrite; other event sources still active."
            )
            return _empty_frame()

        resp.raise_for_status()
        data = resp.json()

        for event in data.get("events", []):
            start_local = event.get("start", {}).get("local")
            if not start_local:
                continue

            venue = event.get("venue") or {}
            rows.append({
                "timestamp": pd.Timestamp(start_local, tz=timezone),
                "venue": venue.get("name", "Unknown"),
                "name": event.get("name", {}).get("text", "Unknown"),
                "expected_attendance": event.get("capacity"),
                "source": "eventbrite",
            })

        pagination = data.get("pagination", {})
        if not pagination.get("has_more_items", False):
            break
        page += 1

    if not rows:
        print("[eventbrite] No events returned for this date range.")
        return _empty_frame()

    df = pd.DataFrame(rows).reset_index(drop=True)
    print(f"[eventbrite] {len(df)} events fetched.")
    return df


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["timestamp", "venue", "name", "expected_attendance", "source"]
    )
