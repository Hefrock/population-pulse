"""Boston.gov civic events fetcher.

Replaces the defunct Eventbrite public-search endpoint. The City of Boston
publishes its event calendar through a Drupal 8+ JSON:API endpoint at
/jsonapi/node/event — no authentication, no API key.

Covers civic gatherings that Ticketmaster misses: marathons, parades,
festivals, community health fairs, public meetings.

Like the Ticketmaster fetcher, this queries upcoming events (today →
today+FORWARD_DAYS) because the API exposes future events only.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import requests

REQUEST_TIMEOUT = 30
PAGE_LIMIT = 50
FORWARD_DAYS = 365

# Drupal JSON:API date fields to try in order of likelihood.
# Boston.gov uses Drupal's recurring-date field module.
_DATE_FIELD_CANDIDATES = [
    "field_event_date_recur",
    "field_intro_date",
    "field_event_date",
    "created",
]


def fetch_events(
    base_url: str,
    start: str,
    end: str,
    timezone: str,
) -> pd.DataFrame:
    """Return upcoming civic events from Boston.gov.

    Parameters
    ----------
    base_url:
        Root URL (e.g. ``https://www.boston.gov``). The JSON:API path is
        appended automatically.
    start, end:
        Pipeline date window (ignored — API only has upcoming events).
    timezone:
        IANA timezone for returned timestamps.
    """
    today = date.today()
    q_end = today + timedelta(days=FORWARD_DAYS)

    rows = []
    offset = 0

    while True:
        try:
            resp = requests.get(
                f"{base_url}/jsonapi/node/event",
                params={
                    "filter[status]": "1",
                    "sort": "field_event_date_recur_value",
                    "page[limit]": PAGE_LIMIT,
                    "page[offset]": offset,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            print(f"[boston_gov] Request failed: {exc}. Skipping civic events.")
            return _empty_frame()

        if resp.status_code in (401, 403, 404):
            print(
                f"[boston_gov] HTTP {resp.status_code} from {base_url}/jsonapi/node/event. "
                "Skipping civic events."
            )
            return _empty_frame()

        try:
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            print(f"[boston_gov] Bad response ({exc}). Skipping civic events.")
            return _empty_frame()

        items = payload.get("data", [])
        if not items:
            break

        for item in items:
            attrs = item.get("attributes", {})
            title = attrs.get("title", "Unknown")
            ts = _extract_date(attrs, timezone)
            if ts is None:
                continue
            # Filter to the forward window client-side.
            if ts.date() < today or ts.date() > q_end:
                continue
            rows.append({
                "timestamp": ts,
                "venue": "Boston, MA",
                "name": title,
                "expected_attendance": None,
                "source": "boston_gov",
            })

        # Drupal JSON:API pagination via next link.
        if payload.get("links", {}).get("next"):
            offset += PAGE_LIMIT
        else:
            break

    if not rows:
        print("[boston_gov] No upcoming civic events found.")
        return _empty_frame()

    df = pd.DataFrame(rows).reset_index(drop=True)
    print(f"[boston_gov] {len(df)} upcoming civic events fetched.")
    return df


def _extract_date(attrs: dict, timezone: str) -> pd.Timestamp | None:
    """Try candidate date fields and return the first parseable timestamp."""
    for field in _DATE_FIELD_CANDIDATES:
        raw = attrs.get(field)
        if not raw:
            continue
        # Recurring date fields come back as a list of dicts with "value".
        if isinstance(raw, list) and raw:
            raw = raw[0].get("value") or raw[0]
        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("start_value")
        try:
            ts = pd.to_datetime(raw, utc=True).tz_convert(timezone)
            return ts.normalize()
        except Exception:
            continue
    return None


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["timestamp", "venue", "name", "expected_attendance", "source"]
    )
