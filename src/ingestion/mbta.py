"""MBTA fetchers.

Two transit signals are available, in priority order:

1. ``fetch_gated_entries`` — **historical daily ridership**. The MBTA/MassDOT
   open-data portal publishes "Gated Station Entries" (fare-gate taps) by
   station and line from 2014 to the present, with no API key. This is the
   signal we actually want for Phase-1 correlation: a real, backfilled daily
   series that lines up against the year of weekly hospital data.

2. ``fetch_live_vehicle_counts`` — a real-time snapshot of in-service vehicles
   per route via the V3 API (https://api-v3.mbta.com). One point per call, so
   only useful when accumulated over many scheduled runs. Kept as a fallback.

``fetch_ridership`` is the interface method the provider calls. It prefers the
historical source, then the live snapshot (when ``MBTA_API_KEY`` is set), then
the bundled sample — always returning ``timestamp``, ``route``, ``value`` and
never raising for an empty/unavailable upstream.

The historical fetcher resolves the live ArcGIS service URL from a stable
*item id* and auto-discovers the date / count / line field names from the
layer metadata, so it keeps working if the published schema shifts. Field
names can also be pinned in config if auto-discovery ever guesses wrong.

Get a free V3 key at https://api-v3.mbta.com/ and put it in your .env as
MBTA_API_KEY (only needed for the live-snapshot fallback).
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import requests

SAMPLE_PATH = Path("data/samples/mbta_ridership_sample.csv")
REQUEST_TIMEOUT = 60
# ArcGIS Online's content endpoint returns the live FeatureServer URL for a
# published item — stable across portal redesigns, unlike the service URL.
ARCGIS_ITEM_API = "https://www.arcgis.com/sharing/rest/content/items/{item_id}"
ARCGIS_QUERY_PAGE = 2000  # ArcGIS default max records per response


def fetch_ridership(
    base_url: str,
    routes: list[str],
    start: str,
    end: str,
    timezone: str,
    historical: dict | None = None,
    live: bool | None = None,
) -> pd.DataFrame:
    """Return transit volume as ``timestamp``, ``route``, ``value``.

    Source priority:
      1. Historical gated station entries (if ``historical`` config is given).
      2. Live vehicle-count snapshot (if ``MBTA_API_KEY`` is set, or live=True).
      3. Bundled sample series.

    Any upstream failure falls through to the next source rather than raising,
    so one flaky endpoint never kills the daily run.
    """
    # 1. Historical ridership — the real time series.
    if historical:
        try:
            df = fetch_gated_entries(start=start, end=end, timezone=timezone, **historical)
            if not df.empty:
                return df
            print("[mbta] Historical gated entries returned no rows; falling back.")
        except Exception as exc:  # noqa: BLE001 - fall back, don't kill the run
            print(f"[mbta] Historical gated entries failed ({exc}); falling back.")

    # 2. Live snapshot.
    if live is None:
        live = bool(os.environ.get("MBTA_API_KEY"))
    if live:
        try:
            snapshot = fetch_live_vehicle_counts(base_url, routes)
            if not snapshot.empty:
                return snapshot
        except Exception as exc:  # noqa: BLE001
            print(f"[mbta] Live vehicle snapshot failed ({exc}); falling back to sample.")

    # 3. Sample.
    print("[mbta] Using sample data.")
    return _load_sample(start, end, timezone)


# --- Historical gated station entries ---------------------------------------

def fetch_gated_entries(
    start: str,
    end: str,
    timezone: str,
    arcgis_item_id: str,
    service_url: str | None = None,
    layer: int = 0,
    date_field: str | None = None,
    count_field: str | None = None,
    line_field: str | None = None,
) -> pd.DataFrame:
    """Return daily gated-entry totals per line from the MBTA open-data portal.

    Aggregates server-side (sum of entries grouped by day and line) so the
    response stays small regardless of how many stations/time-periods underlie
    it. Field names are auto-discovered from the layer metadata unless pinned.

    Returns ``timestamp`` (daily, tz-aware), ``route`` (line name), ``value``
    (entry count). Lines are summed downstream in :mod:`src.analysis.correlate`,
    so per-line rows are fine.
    """
    query_url = f"{_resolve_service_url(arcgis_item_id, service_url)}/{layer}/query"

    fields = _discover_fields(query_url.rsplit("/query", 1)[0])
    date_field = date_field or fields["date"]
    count_field = count_field or fields["count"]
    line_field = line_field or fields["line"]  # may be None if no line column
    if not date_field or not count_field:
        raise ValueError(
            "Could not determine date/count fields on the gated-entries layer; "
            "pin them via config (date_field / count_field)."
        )

    group_fields = [date_field] + ([line_field] if line_field else [])
    where = (
        f"{date_field} >= DATE '{start}' AND {date_field} <= DATE '{end}'"
    )
    out_stats = (
        '[{"statisticType":"sum","onStatisticField":"%s",'
        '"outStatisticFieldName":"entries"}]' % count_field
    )

    rows: list[dict] = []
    offset = 0
    while True:
        resp = requests.get(
            query_url,
            params={
                "where": where,
                "groupByFieldsForStatistics": ",".join(group_fields),
                "outStatistics": out_stats,
                "orderByFields": date_field,
                "resultOffset": offset,
                "resultRecordCount": ARCGIS_QUERY_PAGE,
                "f": "json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise ValueError(f"ArcGIS query error: {payload['error']}")

        features = payload.get("features", [])
        for feat in features:
            attrs = feat.get("attributes", {})
            ts = _parse_arcgis_date(attrs.get(date_field), timezone)
            if ts is None:
                continue
            rows.append({
                "timestamp": ts,
                "route": str(attrs.get(line_field, "all")) if line_field else "all",
                "value": attrs.get("entries"),
            })

        if payload.get("exceededTransferLimit") and features:
            offset += len(features)
            continue
        break

    if not rows:
        return pd.DataFrame(columns=["timestamp", "route", "value"])

    df = pd.DataFrame(rows)
    df = df.dropna(subset=["value"]).reset_index(drop=True)
    print(f"[mbta] {len(df)} daily gated-entry rows fetched (historical).")
    return df


def _resolve_service_url(item_id: str, service_url: str | None) -> str:
    """Return the FeatureServer URL for an ArcGIS item id (or the override)."""
    if service_url:
        return service_url.rstrip("/")
    resp = requests.get(
        ARCGIS_ITEM_API.format(item_id=item_id),
        params={"f": "json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    url = resp.json().get("url")
    if not url:
        raise ValueError(f"ArcGIS item {item_id} has no service URL.")
    return url.rstrip("/")


def _discover_fields(layer_url: str) -> dict[str, str | None]:
    """Inspect layer metadata and guess the date, count, and line fields."""
    resp = requests.get(layer_url, params={"f": "json"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    fields = resp.json().get("fields", [])

    date_field = count_field = line_field = None
    for f in fields:
        name = f.get("name", "")
        low = name.lower()
        ftype = f.get("type", "")
        if date_field is None and (ftype == "esriFieldTypeDate" or "date" in low):
            date_field = name
        if count_field is None and ("entr" in low or "gated" in low) and ftype in (
            "esriFieldTypeInteger", "esriFieldTypeSmallInteger",
            "esriFieldTypeDouble", "esriFieldTypeSingle", "esriFieldTypeBigInteger",
        ):
            count_field = name
        if line_field is None and ("line" in low or "route" in low):
            line_field = name
    return {"date": date_field, "count": count_field, "line": line_field}


def _parse_arcgis_date(value, timezone: str) -> pd.Timestamp | None:
    """ArcGIS dates come back as epoch milliseconds (UTC). Normalize to a day."""
    if value is None:
        return None
    try:
        ts = pd.Timestamp(int(value), unit="ms", tz="UTC").tz_convert(timezone)
    except (ValueError, TypeError):
        ts = pd.to_datetime(value, errors="coerce")
        if ts is pd.NaT:
            return None
        if ts.tzinfo is None:
            ts = ts.tz_localize(timezone)
    return ts.normalize()


# --- Live vehicle snapshot (fallback) ---------------------------------------

def fetch_live_vehicle_counts(base_url: str, routes: list[str]) -> pd.DataFrame:
    """Count in-service vehicles per route right now via the V3 API.

    Returns one row per route with the current timestamp. A "right now" measure,
    so it only builds a series when run repeatedly on a schedule.
    """
    api_key = os.environ.get("MBTA_API_KEY")
    headers = {"x-api-key": api_key} if api_key else {}
    if not api_key:
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
