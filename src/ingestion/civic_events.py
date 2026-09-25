"""Boston.gov civic events fetcher.

Replaces the defunct Eventbrite public-search endpoint. The City of Boston
publishes its event calendar through a Drupal 8+ JSON:API endpoint at
/jsonapi/node/event — no authentication, no API key.

Covers civic gatherings that Ticketmaster misses: marathons, parades,
festivals, community health fairs, public meetings.

Like the Ticketmaster fetcher, this queries upcoming events (today →
today+FORWARD_DAYS) because the API exposes future events only.

As of Sep 2026, this endpoint started intermittently returning a reset
connection or an empty 200 body (two different failure signatures across
consecutive daily runs — see the project's ingestion-health sitrep) instead
of its usual JSON, which fails soft to zero rows via the same path as a real
outage. Two mitigations have been tried:

1. A realistic browser User-Agent instead of `requests`' default
   (`python-requests/X.Y`), on the theory that a common bot-protection
   trigger was at fault. Tested directly against production (a manual
   `workflow_dispatch` run with full GitHub Actions network access): it did
   **not** fix it -- the same "Response ended prematurely" reset still
   occurred, which is a connection-level failure a header change wouldn't
   explain. Left in anyway since it's a harmless, real improvement.
2. Retrying transient failures a few times with backoff before giving up,
   since two different, non-reproducible failure signatures across separate
   daily runs look more like intermittent flakiness (rate-limiting, network
   path issues) than a persistent block -- see `_fetch_page_with_retries`.

If retries don't help either, the remaining unknowns need a human with real
browser access to boston.gov, since this project's sandboxed dev environment
cannot reach the domain to inspect the live response directly -- see the
tracking issue this was filed alongside.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
import requests

REQUEST_TIMEOUT = 30
PAGE_LIMIT = 50
FORWARD_DAYS = 365

# A default `requests` User-Agent (python-requests/X.Y) is a common trigger
# for bot-protection/WAF blocks on public-sector sites. Tested against
# production and did NOT fix the underlying outage (see module docstring),
# but kept as a harmless, real improvement.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/vnd.api+json",
}

# Retries for transient failures (connection resets, empty bodies) -- both
# failure signatures seen against this endpoint since Sep 2026 look
# intermittent rather than a persistent block. Backoff: 2s, then 4s.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2

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
    url = f"{base_url}/jsonapi/node/event"

    rows = []
    offset = 0

    while True:
        payload = _fetch_page_with_retries(
            url,
            {
                "filter[status]": "1",
                "page[limit]": PAGE_LIMIT,
                "page[offset]": offset,
            },
        )
        if payload is None:
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

    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    print(f"[boston_gov] {len(df)} upcoming civic events fetched.")
    return df


class _NonRetryableError(Exception):
    """A 401/403/404 -- a deliberate rejection, not worth retrying."""


def _fetch_page(url: str, params: dict) -> dict:
    """GET one page and return its parsed JSON, or raise on failure."""
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
    if resp.status_code in (401, 403, 404):
        raise _NonRetryableError(f"HTTP {resp.status_code} from {url}")
    resp.raise_for_status()
    return resp.json()


def _fetch_page_with_retries(url: str, params: dict) -> dict | None:
    """Retry transient failures a few times with backoff before giving up.

    Returns the parsed payload, or None if every attempt failed (already
    printed why) -- the caller treats None as "stop paginating, fail soft".
    """
    description = "Unknown error"
    for attempt in range(MAX_ATTEMPTS):
        try:
            return _fetch_page(url, params)
        except _NonRetryableError as exc:
            print(f"[boston_gov] {exc}. Skipping civic events.")
            return None
        except requests.RequestException as exc:
            description = f"Request failed: {exc}"
        except Exception as exc:
            description = f"Bad response ({exc})"

        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))

    print(f"[boston_gov] {description} after {MAX_ATTEMPTS} attempts. Skipping civic events.")
    return None


def _extract_date(attrs: dict, timezone: str) -> pd.Timestamp | None:
    """Try candidate date fields and return the first parseable timestamp."""
    for field in _DATE_FIELD_CANDIDATES:
        raw = attrs.get(field)
        if not raw:
            continue
        # Recurring date fields come back as a list of dicts with "value",
        # or occasionally as a list of bare ISO-date strings.
        if isinstance(raw, list) and raw:
            item = raw[0]
            raw = item.get("value") or item if isinstance(item, dict) else item
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
