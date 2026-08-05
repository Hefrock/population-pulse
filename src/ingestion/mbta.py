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

import io
import os
import zipfile
from pathlib import Path

import pandas as pd
import requests

from src.ingestion.sample_window import shift_sample_to_window

SAMPLE_PATH = Path("data/samples/mbta_ridership_sample.csv")
SERVICE_LEVEL_SAMPLE_PATH = Path("data/samples/mbta_service_level_sample.csv")
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 180  # CSV downloads can be tens of MB
# ArcGIS Online's content endpoint describes a published item. Feature-service
# items expose a service ``url``; file items (CSV) expose their bytes at /data.
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

    The MBTA publishes this as one of two ArcGIS item types, so we handle both:

    * **Feature Service** — query server-side, summing entries grouped by day
      and line, so the response stays small.
    * **CSV file item** (what the "Gated Station Entries" datasets actually are)
      — download the CSV via the item's ``/data`` endpoint and aggregate in
      pandas.

    Field names are auto-discovered (overridable via config). Returns
    ``timestamp`` (daily, tz-aware), ``route`` (line), ``value`` (entry count).
    Lines are summed downstream in :mod:`src.analysis.correlate`, so per-line
    rows are fine.
    """
    url = service_url or _item_service_url(arcgis_item_id)
    if url:
        return _fetch_via_feature_service(
            url, layer, start, end, timezone, date_field, count_field, line_field
        )
    # File item: download the CSV and aggregate client-side.
    return _fetch_via_csv(
        arcgis_item_id, start, end, timezone, date_field, count_field, line_field
    )


# --- ArcGIS item resolution -------------------------------------------------

def _item_service_url(item_id: str) -> str | None:
    """Return the item's Feature Service URL, or None for a file (CSV) item."""
    resp = requests.get(
        ARCGIS_ITEM_API.format(item_id=item_id),
        params={"f": "json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    meta = resp.json()
    url = (meta.get("url") or "").rstrip("/")
    if not url:
        # Diagnostic so a single run tells us exactly what the item is.
        print(
            f"[mbta] ArcGIS item {item_id} type={meta.get('type')!r} "
            f"has no service URL — using CSV download path."
        )
        return None
    return url


# --- Feature-service path ---------------------------------------------------

def _fetch_via_feature_service(
    service_url, layer, start, end, timezone, date_field, count_field, line_field
) -> pd.DataFrame:
    query_url = f"{service_url}/{layer}/query"
    fields = _discover_fields(f"{service_url}/{layer}")
    date_field = date_field or fields["date"]
    count_field = count_field or fields["count"]
    line_field = line_field or fields["line"]
    if not date_field or not count_field:
        raise ValueError(
            "Could not determine date/count fields on the gated-entries layer; "
            "pin them via config (date_field / count_field)."
        )

    group_fields = [date_field] + ([line_field] if line_field else [])
    where = f"{date_field} >= DATE '{start}' AND {date_field} <= DATE '{end}'"
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
    df = pd.DataFrame(rows).dropna(subset=["value"]).reset_index(drop=True)
    print(f"[mbta] {len(df)} daily gated-entry rows fetched (feature service).")
    return df


def _discover_fields(layer_url: str) -> dict[str, str | None]:
    """Inspect layer metadata and guess the date, count, and line fields."""
    resp = requests.get(layer_url, params={"f": "json"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    fields = resp.json().get("fields", [])

    numeric_types = {
        "esriFieldTypeInteger", "esriFieldTypeSmallInteger", "esriFieldTypeDouble",
        "esriFieldTypeSingle", "esriFieldTypeBigInteger",
    }
    date_field = count_field = line_field = None
    for f in fields:
        name = f.get("name", "")
        low = name.lower()
        ftype = f.get("type", "")
        if date_field is None and (ftype == "esriFieldTypeDate" or "date" in low):
            date_field = name
        if count_field is None and ("entr" in low or "gated" in low) and ftype in numeric_types:
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


# --- CSV-file path ----------------------------------------------------------

def _fetch_via_csv(
    item_id, start, end, timezone, date_field, count_field, line_field
) -> pd.DataFrame:
    """Download the item's CSV data and aggregate to daily totals per line."""
    resp = requests.get(
        f"{ARCGIS_ITEM_API.format(item_id=item_id)}/data",
        timeout=DOWNLOAD_TIMEOUT,
    )
    resp.raise_for_status()
    raw = _read_tabular(resp.content)

    detected = _detect_csv_fields(raw)
    date_col = date_field or detected["date"]
    count_col = count_field or detected["count"]
    line_col = line_field or detected["line"]
    print(f"[mbta] CSV columns={list(raw.columns)}; using date={date_col!r} "
          f"count={count_col!r} line={line_col!r}")
    if not date_col or not count_col:
        raise ValueError(
            "Could not determine date/count columns in the gated-entries CSV; "
            "pin them via config (date_field / count_field)."
        )

    df = raw[[c for c in (date_col, line_col, count_col) if c]].copy()
    df["_day"] = pd.to_datetime(df[date_col], errors="coerce").dt.normalize()
    df = df.dropna(subset=["_day"])

    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    df = df[(df["_day"] >= lo) & (df["_day"] <= hi)]
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "route", "value"])

    df[count_col] = pd.to_numeric(df[count_col], errors="coerce")
    group_cols = ["_day"] + ([line_col] if line_col else [])
    agg = df.groupby(group_cols, dropna=False)[count_col].sum().reset_index()

    out = pd.DataFrame({
        "timestamp": agg["_day"].dt.tz_localize(timezone),
        "route": agg[line_col].astype(str) if line_col else "all",
        "value": agg[count_col],
    }).dropna(subset=["value"]).reset_index(drop=True)
    print(f"[mbta] {len(out)} daily gated-entry rows fetched (CSV, "
          f"{out['timestamp'].dt.date.min()}..{out['timestamp'].dt.date.max()}).")
    return out


def _read_tabular(content: bytes) -> pd.DataFrame:
    """Read CSV bytes, transparently handling a zipped bundle of CSVs."""
    if content[:2] == b"PK":  # zip magic number
        frames = []
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".csv"):
                    with zf.open(name) as fh:
                        frames.append(pd.read_csv(fh))
        if not frames:
            raise ValueError("Zip archive contained no CSV files.")
        return pd.concat(frames, ignore_index=True)
    return pd.read_csv(io.BytesIO(content))


def _detect_csv_fields(df: pd.DataFrame) -> dict[str, str | None]:
    """Guess the date, count, and line columns from a CSV's headers/dtypes."""
    date_col = count_col = line_col = None
    for col in df.columns:
        low = str(col).lower()
        if date_col is None and "date" in low:
            date_col = col
        if count_col is None and ("entr" in low or "gated" in low or "ridership" in low):
            count_col = col
        if line_col is None and ("line" in low or "route" in low):
            line_col = col
    # Fall back: first numeric column for the count if no name matched.
    if count_col is None:
        for col in df.columns:
            if pd.api.types.is_numeric_dtype(df[col]):
                count_col = col
                break
    return {"date": date_col, "count": count_col, "line": line_col}


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


def fetch_transit_service_level(
    base_url: str, routes: list[str], start: str, end: str, timezone: str,
) -> pd.DataFrame:
    """Accumulated version of ``fetch_live_vehicle_counts`` -- a *separate*
    signal, not a fallback within ``fetch_ridership``.

    ``fetch_ridership``'s historical gated-entries source has a genuine 1-2
    month publication lag (MBTA's own documented cadence, not a bug in this
    fetcher), so ``transit`` is structurally always somewhat stale. This
    fetcher's data is current as of every daily run, but it measures a
    fundamentally different thing -- vehicles in service right now (a stock),
    not fare-gate taps (a flow) -- so it must accumulate as its own signal,
    never merged into ``transit``'s history: ``align()`` sums every row
    sharing a timestamp regardless of route, so splicing a stock measure into
    a flow measure's column would corrupt the composite's meaning even though
    the two sources' route-label vocabularies don't even overlap (checked:
    gated-entries uses "Red Line"/"Green Line"/etc., the V3 API config uses
    "Red"/"Green-B"/etc., so they'd never collide via key-based dedup either
    way -- the risk here was always semantic, not a literal overwrite).

    Falls back to the bundled sample if the V3 API is unreachable, matching
    every other fetcher's fail-soft pattern. Returns ``timestamp``, ``route``,
    ``value`` (vehicle-in-service count), same shape as ``fetch_ridership``.
    """
    try:
        snapshot = fetch_live_vehicle_counts(base_url, routes)
        if not snapshot.empty:
            return snapshot
        print("[mbta] Live vehicle snapshot returned no rows; falling back to sample.")
    except Exception as exc:  # noqa: BLE001 - fall back, don't kill the run
        print(f"[mbta] Live vehicle snapshot failed ({exc}); falling back to sample.")
    return _load_service_level_sample(start, end, timezone)


def _load_service_level_sample(start: str, end: str, timezone: str) -> pd.DataFrame:
    """Load and window the bundled service-level sample series."""
    if not SERVICE_LEVEL_SAMPLE_PATH.exists():
        raise FileNotFoundError(
            f"Sample data missing at {SERVICE_LEVEL_SAMPLE_PATH}. "
            "Run `python -m src.ingestion.make_samples` to regenerate it."
        )
    df = pd.read_csv(SERVICE_LEVEL_SAMPLE_PATH, parse_dates=["timestamp"])
    df = shift_sample_to_window(df, start, end)
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(timezone)
    mask = (df["timestamp"] >= pd.Timestamp(start, tz=timezone)) & (
        df["timestamp"] <= pd.Timestamp(end, tz=timezone)
    )
    return df.loc[mask].reset_index(drop=True)


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
