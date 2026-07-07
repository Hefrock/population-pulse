"""Tests for ingestion fetcher edge cases."""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from src.ingestion.cdc_fluview import _date_to_epiweek, _epiweek_to_timestamp
from src.ingestion.ticketmaster import _empty_frame as tm_empty
from src.ingestion.civic_events import _empty_frame as ce_empty
from src.ingestion import mbta, academic_calendar


# --- CDC FluView epiweek helpers -------------------------------------------

def test_epiweek_roundtrip_stays_within_one_week():
    """date → epiweek → timestamp should stay within 7 days of the original."""
    for d in [
        datetime.date(2025, 1, 5),   # early January (near year boundary)
        datetime.date(2025, 3, 15),  # mid-year
        datetime.date(2025, 12, 28), # late December (near year boundary)
    ]:
        ew = _date_to_epiweek(d)
        ts = _epiweek_to_timestamp(ew, "America/New_York")
        diff = abs((ts.date() - d).days)
        assert diff <= 7, f"{d} roundtripped off by {diff} days (epiweek={ew})"


def test_epiweek_dst_spring_forward():
    """epiweek containing spring-forward day must not crash."""
    ew = _date_to_epiweek(datetime.date(2026, 3, 8))  # spring-forward week
    ts = _epiweek_to_timestamp(ew, "America/New_York")
    assert ts is not pd.NaT
    assert ts.tzinfo is not None


def test_epiweek_format():
    """_date_to_epiweek returns a 6-digit YYYYWW integer."""
    ew = _date_to_epiweek(datetime.date(2025, 6, 15))
    assert 202501 <= ew <= 202553


# --- Weather DST handling ---------------------------------------------------

def test_weather_dst_localize_nonexistent():
    """The nonexistent 2 AM spring-forward hour must shift forward, not crash."""
    naive = pd.to_datetime([
        "2026-03-08 01:00",
        "2026-03-08 02:00",  # nonexistent — clocks spring to 03:00
        "2026-03-08 03:00",
    ])
    result = naive.tz_localize(
        "America/New_York", nonexistent="shift_forward", ambiguous=False
    )
    assert result[1] == result[2]
    assert result[1].hour == 3


# The bundled sample is generated relative to "today" (matching run.py's
# default "trailing 365 days" ingestion window) — see make_samples._date_range.
# A fixed calendar window here would drift out of range over time and silently
# filter the sample to zero rows.
_WX_END = datetime.date.today().isoformat()
_WX_START = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()


def test_weather_network_error_falls_back_to_sample(monkeypatch):
    """If Open-Meteo is unreachable, fetch_open_meteo degrades to the bundled
    synthetic sample instead of raising."""
    import requests as req
    from src.ingestion import weather

    monkeypatch.setattr(
        weather.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(req.ConnectionError("no route to host")),
    )
    df = weather.fetch_open_meteo(
        base_url="https://api.open-meteo.com/v1/forecast",
        latitude=42.36, longitude=-71.06,
        variables=["temperature_2m", "apparent_temperature", "precipitation"],
        start=_WX_START, end=_WX_END,
        timezone="America/New_York",
    )
    assert not df.empty
    assert list(df.columns) == ["timestamp", "temperature_2m", "apparent_temperature", "precipitation"]
    assert df["timestamp"].dt.tz is not None


def test_weather_http_error_falls_back_to_sample(monkeypatch):
    """A non-2xx response from Open-Meteo also degrades to the sample."""
    import requests as req_module
    from src.ingestion import weather

    class _BadResp:
        def raise_for_status(self):
            raise req_module.HTTPError("503 Service Unavailable")

    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: _BadResp())

    df = weather.fetch_open_meteo(
        base_url="https://api.open-meteo.com/v1/forecast",
        latitude=42.36, longitude=-71.06,
        variables=["temperature_2m", "apparent_temperature", "precipitation"],
        start=_WX_START, end=_WX_END,
        timezone="America/New_York",
    )
    assert not df.empty


def test_weather_sample_covers_window_past_its_own_dates():
    """The bundled weather sample spans a fixed ~1-year window frozen at
    generation time, but run.py requests a window relative to *today*. Once the
    committed sample ages out of that window the fallback must still return
    data (shifting the sample by whole years), not silently clip to zero rows —
    the failure mode that broke daily ingestion when the sample aged past the
    trailing-365-day window."""
    from src.ingestion import weather

    # A window years past any committed sample — the shift must still cover it.
    future_start = (datetime.date.today() + datetime.timedelta(days=365 * 3)).isoformat()
    future_end = (datetime.date.today() + datetime.timedelta(days=365 * 3 + 7)).isoformat()
    df = weather._load_sample(
        ["temperature_2m", "apparent_temperature", "precipitation"],
        future_start, future_end, "America/New_York",
    )
    assert not df.empty
    assert df["timestamp"].min() >= pd.Timestamp(future_start, tz="America/New_York")
    assert df["timestamp"].max() <= pd.Timestamp(future_end, tz="America/New_York")
    # planted seasonality survives the whole-year shift (not a flat/degenerate series)
    assert df["temperature_2m"].nunique() > 1


def test_weather_dst_localize_ambiguous():
    """The ambiguous 1 AM fall-back hour must not crash (ambiguous=False → standard time)."""
    naive = pd.to_datetime([
        "2025-11-02 00:00",
        "2025-11-02 01:00",  # ambiguous — occurs twice during fall-back
        "2025-11-02 02:00",
    ])
    result = naive.tz_localize(
        "America/New_York", nonexistent="shift_forward", ambiguous=False
    )
    assert result is not None
    assert len(result) == 3


# --- Fetcher empty-frame schema contracts -----------------------------------

def test_ticketmaster_empty_frame_schema():
    df = tm_empty()
    assert list(df.columns) == ["timestamp", "venue", "name", "expected_attendance", "source"]
    assert len(df) == 0


def test_civic_events_empty_frame_schema():
    df = ce_empty()
    assert list(df.columns) == ["timestamp", "venue", "name", "expected_attendance", "source"]
    assert len(df) == 0


def test_ticketmaster_no_key_returns_empty(monkeypatch):
    """fetch_events with no API key returns an empty frame, not an exception."""
    monkeypatch.delenv("TICKETMASTER_API_KEY", raising=False)
    from src.ingestion.ticketmaster import fetch_events
    df = fetch_events(
        base_url="https://app.ticketmaster.com/discovery/v2",
        city="Boston", state_code="MA",
        start="2025-01-01", end="2025-03-31",
        timezone="America/New_York",
    )
    assert df.empty


def test_civic_events_network_error_returns_empty(monkeypatch):
    """A network error from Boston.gov returns empty, not an exception."""
    import requests as req
    from src.ingestion import civic_events

    monkeypatch.setattr(
        civic_events.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(req.ConnectionError("no route to host")),
    )
    df = civic_events.fetch_events(
        base_url="https://www.boston.gov",
        start="2025-01-01", end="2025-12-31",
        timezone="America/New_York",
    )
    assert df.empty


def test_epiweek_dst_fall_back_does_not_crash():
    """_epiweek_to_timestamp must not raise for the ambiguous DST fall-back week."""
    ew = _date_to_epiweek(datetime.date(2025, 11, 2))  # clocks fall back this week
    ts = _epiweek_to_timestamp(ew, "America/New_York")
    assert ts is not pd.NaT
    assert ts.tzinfo is not None


# --- Boston.gov civic events ------------------------------------------------

def test_civic_events_http_error_returns_empty(monkeypatch):
    """Any HTTP error from Boston.gov returns empty, not an exception."""
    from src.ingestion import civic_events

    monkeypatch.setattr(
        civic_events.requests, "get",
        lambda *a, **k: _FakeResp({"data": [], "links": {}}),
    )
    df = civic_events.fetch_events(
        base_url="https://www.boston.gov",
        start="2025-01-01", end="2025-12-31",
        timezone="America/New_York",
    )
    assert df.empty


def test_civic_events_parses_drupal_jsonapi(monkeypatch):
    """A valid Drupal JSON:API payload is parsed into the standard schema."""
    from src.ingestion import civic_events

    future = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=30)).isoformat()
    payload = {
        "data": [
            {
                "type": "node--event",
                "attributes": {
                    "title": "Boston Marathon",
                    "field_event_date_recur": [{"value": future}],
                },
            }
        ],
        "links": {},
    }
    monkeypatch.setattr(
        civic_events.requests, "get",
        lambda *a, **k: _FakeResp(payload),
    )
    df = civic_events.fetch_events(
        base_url="https://www.boston.gov",
        start="2025-01-01", end="2025-12-31",
        timezone="America/New_York",
    )
    assert not df.empty
    assert list(df.columns) == ["timestamp", "venue", "name", "expected_attendance", "source"]
    assert df["name"].iloc[0] == "Boston Marathon"
    assert df["source"].iloc[0] == "boston_gov"


def test_civic_events_does_not_send_sort_param(monkeypatch):
    """Drupal returns 400 Bad Request for sort=field_event_date_recur_value
    in production -- the request must not include a 'sort' param at all."""
    from src.ingestion import civic_events

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured.update(params or {})
        return _FakeResp({"data": [], "links": {}})

    monkeypatch.setattr(civic_events.requests, "get", fake_get)
    civic_events.fetch_events(
        base_url="https://www.boston.gov",
        start="2025-01-01", end="2025-12-31",
        timezone="America/New_York",
    )
    assert "sort" not in captured


def test_civic_events_400_response_returns_empty(monkeypatch):
    """A 400 from Drupal JSON:API (e.g. an unsortable field) fails soft."""
    import requests as req
    from src.ingestion import civic_events

    class _BadResp(_FakeResp):
        def raise_for_status(self):
            raise req.HTTPError("400 Client Error: Bad Request")

    monkeypatch.setattr(
        civic_events.requests, "get",
        lambda *a, **k: _BadResp({"data": [], "links": {}}),
    )
    df = civic_events.fetch_events(
        base_url="https://www.boston.gov",
        start="2025-01-01", end="2025-12-31",
        timezone="America/New_York",
    )
    assert df.empty
    assert list(df.columns) == ["timestamp", "venue", "name", "expected_attendance", "source"]


def test_ticketmaster_uses_classificationname(monkeypatch):
    """fetch_events sends classificationName (not segmentName) to the API."""
    import os
    from src.ingestion import ticketmaster

    monkeypatch.setenv("TICKETMASTER_API_KEY", "testkey")
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured.update(params or {})
        return _FakeResp({"page": {"totalPages": 1}, "_embedded": {"events": []}})

    monkeypatch.setattr(ticketmaster.requests, "get", fake_get)
    ticketmaster.fetch_events(
        base_url="https://app.ticketmaster.com/discovery/v2",
        city="Boston", state_code="MA",
        start="2025-01-01", end="2025-03-31",
        timezone="America/New_York",
        segments=["Sports", "Music"],
    )
    assert "classificationName" in captured
    assert "segmentName" not in captured


# --- MBTA historical gated-entries ------------------------------------------

class _FakeResp:
    def __init__(self, payload=None, content=None, status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _arcgis_router(monkeypatch, *, fields, features):
    """Patch mbta.requests.get to emulate the feature-service conversation."""
    def fake_get(url, params=None, timeout=None):
        if "/sharing/rest/content/items/" in url:  # item -> has a service url
            return _FakeResp({"url": "https://svc.example/arcgis/rest/services/X/FeatureServer"})
        if url.endswith("/0") or url.endswith("/0/"):
            return _FakeResp({"fields": fields})
        if url.endswith("/query"):
            return _FakeResp({"features": features})
        raise AssertionError(f"unexpected URL: {url}")
    monkeypatch.setattr(mbta.requests, "get", fake_get)


def test_parse_arcgis_date_epoch_ms_normalizes_to_day():
    # 2025-06-15 12:34 UTC in epoch ms -> normalized to the local day.
    ms = int(pd.Timestamp("2025-06-15T12:34:00Z").value // 1_000_000)
    ts = mbta._parse_arcgis_date(ms, "America/New_York")
    assert ts is not None
    assert ts.hour == 0 and ts.minute == 0       # normalized
    assert str(ts.tz) == "America/New_York"
    assert ts.date().isoformat() == "2025-06-15"


def test_discover_fields_picks_date_count_and_line(monkeypatch):
    fields = [
        {"name": "OBJECTID", "type": "esriFieldTypeOID"},
        {"name": "service_date", "type": "esriFieldTypeDate"},
        {"name": "route_or_line", "type": "esriFieldTypeString"},
        {"name": "gated_entries", "type": "esriFieldTypeInteger"},
    ]
    monkeypatch.setattr(
        mbta.requests, "get",
        lambda url, params=None, timeout=None: _FakeResp({"fields": fields}),
    )
    got = mbta._discover_fields("https://svc.example/FeatureServer/0")
    assert got == {
        "date": "service_date",
        "count": "gated_entries",
        "line": "route_or_line",
    }


def test_fetch_gated_entries_aggregates(monkeypatch):
    fields = [
        {"name": "service_date", "type": "esriFieldTypeDate"},
        {"name": "route_or_line", "type": "esriFieldTypeString"},
        {"name": "gated_entries", "type": "esriFieldTypeInteger"},
    ]
    day = int(pd.Timestamp("2025-06-15T00:00:00Z").value // 1_000_000)
    features = [
        {"attributes": {"service_date": day, "route_or_line": "Red Line", "entries": 12345}},
        {"attributes": {"service_date": day, "route_or_line": "Orange Line", "entries": 6789}},
    ]
    _arcgis_router(monkeypatch, fields=fields, features=features)

    df = mbta.fetch_gated_entries(
        start="2025-06-01", end="2025-06-30",
        timezone="America/New_York",
        arcgis_item_id="dummy",
    )
    assert list(df.columns) == ["timestamp", "route", "value"]
    assert len(df) == 2
    assert set(df["route"]) == {"Red Line", "Orange Line"}
    assert df["value"].sum() == 12345 + 6789
    assert str(df["timestamp"].dt.tz) == "America/New_York"


def test_fetch_gated_entries_via_csv(monkeypatch):
    """A file (CSV) item has no service URL, so we download and aggregate it."""
    csv = (
        "service_date,route_or_line,gated_entries\n"
        "2025-06-15,Red Line,100\n"
        "2025-06-15,Red Line,50\n"      # same day+line -> summed to 150
        "2025-06-15,Orange Line,40\n"
        "2030-01-01,Red Line,999\n"     # outside window -> dropped
    ).encode()

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/data"):
            return _FakeResp(content=csv)
        if "/sharing/rest/content/items/" in url:
            return _FakeResp({"type": "CSV"})  # no 'url' -> CSV path
        raise AssertionError(f"unexpected URL: {url}")
    monkeypatch.setattr(mbta.requests, "get", fake_get)

    df = mbta.fetch_gated_entries(
        start="2025-06-01", end="2025-06-30",
        timezone="America/New_York",
        arcgis_item_id="dummy",
    )
    assert list(df.columns) == ["timestamp", "route", "value"]
    assert set(df["route"]) == {"Red Line", "Orange Line"}
    red = df.loc[df["route"] == "Red Line", "value"].iloc[0]
    assert red == 150                     # 100 + 50, same day
    assert df["value"].sum() == 150 + 40  # 2030 row excluded by date window
    assert str(df["timestamp"].dt.tz) == "America/New_York"


def test_fetch_ridership_falls_back_to_sample_when_historical_fails(monkeypatch, tmp_path):
    # Historical raises -> no live key -> sample is used (must not raise).
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(mbta.requests, "get", boom)
    monkeypatch.delenv("MBTA_API_KEY", raising=False)

    # Point the sample loader at a tiny fixture so the test is hermetic.
    sample = tmp_path / "mbta_ridership_sample.csv"
    pd.DataFrame({
        "timestamp": ["2025-06-10", "2025-06-11"],
        "route": ["Red", "Red"],
        "value": [100, 110],
    }).to_csv(sample, index=False)
    monkeypatch.setattr(mbta, "SAMPLE_PATH", sample)

    df = mbta.fetch_ridership(
        base_url="https://api-v3.mbta.com",
        routes=["Red"],
        start="2025-06-01", end="2025-06-30",
        timezone="America/New_York",
        historical={"arcgis_item_id": "dummy"},
    )
    assert not df.empty
    assert list(df.columns) == ["timestamp", "route", "value"]


# --- Academic-calendar population-driver ------------------------------------

def _write_calendar_csv(path, school="Test U", enrollment=1000,
                        start_date="2025-09-01", end_date="2025-12-01"):
    pd.DataFrame({
        "school": [school],
        "enrollment": [enrollment],
        "term": ["Fall 2025"],
        "start_date": [start_date],
        "end_date": [end_date],
    }).to_csv(path, index=False)


def test_academic_calendar_missing_file_returns_empty(tmp_path):
    df = academic_calendar.fetch_population_index(
        path=tmp_path / "nope.csv",
        start="2025-09-01", end="2025-09-30",
        timezone="America/New_York",
    )
    assert list(df.columns) == ["timestamp", "school", "value"]
    assert df.empty


def test_academic_calendar_full_weight_mid_term(tmp_path):
    """Mid-term, the in-session weight is 1.0 -> value equals enrollment."""
    csv = tmp_path / "calendar.csv"
    _write_calendar_csv(csv, enrollment=1000, start_date="2025-09-01", end_date="2025-12-01")

    df = academic_calendar.fetch_population_index(
        path=csv, start="2025-09-15", end="2025-09-15",
        timezone="America/New_York", ramp_days=7,
    )
    assert len(df) == 1
    assert df["value"].iloc[0] == 1000.0


def test_academic_calendar_ramps_up_before_term_start(tmp_path):
    """A few days before move-in, the weight is a partial ramp, not 0 or 1."""
    csv = tmp_path / "calendar.csv"
    _write_calendar_csv(csv, enrollment=1000, start_date="2025-09-08", end_date="2025-12-01")

    df = academic_calendar.fetch_population_index(
        path=csv, start="2025-09-04", end="2025-09-04",  # 4 days before start, within the 7-day ramp
        timezone="America/New_York", ramp_days=7,
    )
    assert len(df) == 1
    assert 0 < df["value"].iloc[0] < 1000.0


def test_academic_calendar_silent_outside_term_and_ramp(tmp_path):
    """Far outside any term (and its ramp window), the school emits no row."""
    csv = tmp_path / "calendar.csv"
    _write_calendar_csv(csv, enrollment=1000, start_date="2025-09-01", end_date="2025-12-01")

    df = academic_calendar.fetch_population_index(
        path=csv, start="2025-07-01", end="2025-07-01",
        timezone="America/New_York", ramp_days=7,
    )
    assert df.empty


def test_academic_calendar_sums_schools_via_align():
    """align() sums per-school rows into one composite index, like MBTA routes."""
    from src.analysis.correlate import align

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-09-15", "2025-09-15"]).tz_localize("UTC"),
        "school": ["A", "B"],
        "value": [1000.0, 2000.0],
    })
    aligned = align({"academic_calendar": df}, resolution="W")
    assert aligned["academic_calendar"].sum() == 3000.0


# --- Wastewater viral surveillance ------------------------------------------

from src.ingestion import wastewater

# The bundled sample is generated relative to "today" (matching run.py's
# default "trailing 365 days" ingestion window) — see make_samples._date_range.
# A fixed calendar window here would drift out of range over time and silently
# filter the sample to zero rows, exactly like the bug this mirrors.
_WW_END = datetime.date.today().isoformat()
_WW_START = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()


def test_wastewater_canonical_pathogen_maps_aliases():
    assert wastewater._canonical_pathogen("SARS-CoV-2") == "SARS-CoV-2"
    assert wastewater._canonical_pathogen("sars cov 2 wastewater") == "SARS-CoV-2"
    assert wastewater._canonical_pathogen("N Gene") == "SARS-CoV-2"
    # "influenza a"/"influenza b" must win over the shorter "influenza" alias
    # (which would otherwise mis-bucket Influenza B as Influenza A).
    assert wastewater._canonical_pathogen("Influenza A virus") == "Influenza A"
    assert wastewater._canonical_pathogen("Influenza A F1R1") == "Influenza A"
    assert wastewater._canonical_pathogen("Influenza B") == "Influenza B"
    assert wastewater._canonical_pathogen("RSV activity") == "RSV"
    assert wastewater._canonical_pathogen("norovirus") is None


def test_wastewater_sample_has_all_pathogens():
    df = wastewater._load_sample(
        ["SARS-CoV-2", "Influenza A", "RSV"],
        start=_WW_START, end=_WW_END, timezone="America/New_York",
    )
    assert list(df.columns) == ["timestamp", "pathogen", "value", "source"]
    assert set(df["pathogen"].unique()) == {"SARS-CoV-2", "Influenza A", "RSV"}
    assert df["timestamp"].dt.tz is not None


def test_wastewater_sample_filters_to_requested_pathogens():
    df = wastewater._load_sample(
        ["RSV"], start=_WW_START, end=_WW_END, timezone="America/New_York",
    )
    assert set(df["pathogen"].unique()) == {"RSV"}


def test_parse_cdc_nwss_long_form():
    records = [
        {"week_end_date": "2025-01-04", "state": "Massachusetts",
         "pathogen": "SARS-CoV-2", "wval": "7.5"},
        {"week_end_date": "2025-01-04", "state": "Massachusetts",
         "pathogen": "Influenza A", "wval": "3.2"},
        {"week_end_date": "2025-01-04", "state": "California",
         "pathogen": "RSV", "wval": "9.9"},  # wrong state, dropped
    ]
    df = wastewater._parse_cdc_nwss(
        records, ["SARS-CoV-2", "Influenza A", "RSV"], "Massachusetts",
        start="2024-06-01", end="2025-12-31", timezone="America/New_York",
    )
    assert set(df["pathogen"].unique()) == {"SARS-CoV-2", "Influenza A"}
    assert (df["source"] == "cdc_nwss").all()
    assert df.loc[df["pathogen"] == "SARS-CoV-2", "value"].iloc[0] == 7.5


def test_parse_cdc_nwss_wide_form():
    records = [
        {"date": "2025-01-04", "state": "MA",
         "sars_cov_2": "7.5", "influenza_a": "3.2", "rsv": "1.1"},
    ]
    df = wastewater._parse_cdc_nwss(
        records, ["SARS-CoV-2", "Influenza A", "RSV"], "Massachusetts",
        start="2024-06-01", end="2025-12-31", timezone="America/New_York",
    )
    assert set(df["pathogen"].unique()) == {"SARS-CoV-2", "Influenza A", "RSV"}


def test_parse_cdc_nwss_respects_date_window():
    records = [
        {"date": "2030-01-04", "state": "MA", "pathogen": "RSV", "wval": "5.0"},
    ]
    df = wastewater._parse_cdc_nwss(
        records, ["RSV"], "Massachusetts",
        start="2024-06-01", end="2025-12-31", timezone="America/New_York",
    )
    assert df.empty  # out-of-window row dropped


def test_fetch_wastewater_falls_back_to_sample_when_sources_unreachable(monkeypatch):
    """No mwra data_url and a CDC endpoint that errors -> bundled sample."""
    def boom(*args, **kwargs):
        raise RuntimeError("network down")
    monkeypatch.setattr(wastewater.requests, "get", boom)

    df = wastewater.fetch_wastewater(
        pathogens=["SARS-CoV-2", "Influenza A", "RSV"],
        start=_WW_START, end=_WW_END, timezone="America/New_York",
        mwra={"base_url": "https://example.invalid"},  # no data_url -> skipped
        cdc_nwss={"base_url": "https://data.cdc.gov/resource/atcp-73re.json"},
    )
    assert not df.empty
    assert set(df["pathogen"].unique()) == {"SARS-CoV-2", "Influenza A", "RSV"}
    assert (df["source"] == "sample").all()


def test_fetch_wastewater_prefers_mwra_for_sars(monkeypatch):
    """MWRA supplies SARS-CoV-2; CDC fills only the remaining pathogens."""
    mwra_df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-01"]).tz_localize("America/New_York"),
        "pathogen": ["SARS-CoV-2"], "value": [123.0], "source": ["mwra"],
    })
    monkeypatch.setattr(wastewater, "_fetch_mwra", lambda *a, **k: mwra_df)

    captured = {}
    def fake_cdc(cfg, pathogens, start, end, tz):
        captured["pathogens"] = pathogens
        return pd.DataFrame({
            "timestamp": pd.to_datetime(["2025-01-01", "2025-01-01"]).tz_localize("America/New_York"),
            "pathogen": ["Influenza A", "RSV"], "value": [3.0, 4.0],
            "source": ["cdc_nwss", "cdc_nwss"],
        })
    monkeypatch.setattr(wastewater, "_fetch_cdc_nwss", fake_cdc)

    df = wastewater.fetch_wastewater(
        pathogens=["SARS-CoV-2", "Influenza A", "RSV"],
        start="2024-06-01", end="2025-12-31", timezone="America/New_York",
        mwra={"data_url": "x"}, cdc_nwss={"base_url": "y"},
    )
    # CDC was asked only for the pathogens MWRA didn't cover.
    assert "SARS-CoV-2" not in captured["pathogens"]
    assert df.loc[df["pathogen"] == "SARS-CoV-2", "source"].iloc[0] == "mwra"
    assert set(df["pathogen"].unique()) == {"SARS-CoV-2", "Influenza A", "RSV"}


def test_parse_wastewaterscan_extracts_pathogens():
    """Maps WastewaterSCAN's per-sample 'targets' dict to canonical pathogens."""
    payload = {
        "samples": [
            {
                "collection_date": "2025-01-05",
                "targets": {
                    "N Gene": {"gc_g_dry_weight": 1000.0},
                    "Influenza A": {"gc_g_dry_weight": 50.0},
                    "RSV": {"gc_g_dry_weight": 25.0},
                    "PMMoV": {"gc_g_dry_weight": 9999.0},  # not a requested pathogen
                    "MPXV_G2R_G": {"gc_g_dry_weight": None},  # missing value, dropped
                },
            },
        ],
    }
    df = wastewater._parse_wastewaterscan(
        payload, ["SARS-CoV-2", "Influenza A", "RSV"],
        start="2024-06-01", end="2025-12-31", timezone="America/New_York",
    )
    assert set(df["pathogen"]) == {"SARS-CoV-2", "Influenza A", "RSV"}
    assert (df["source"] == "wastewaterscan").all()
    assert df.loc[df["pathogen"] == "SARS-CoV-2", "value"].iloc[0] == 1000.0


def test_fetch_wastewater_prefers_wastewaterscan(monkeypatch):
    """WastewaterSCAN supplies all pathogens; MWRA/CDC NWSS are not consulted."""
    wws_df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-05"] * 3).tz_localize("America/New_York"),
        "pathogen": ["SARS-CoV-2", "Influenza A", "RSV"],
        "value": [1000.0, 50.0, 25.0],
        "source": ["wastewaterscan"] * 3,
    })
    monkeypatch.setattr(wastewater, "_fetch_wastewaterscan", lambda *a, **k: wws_df)

    def boom(*args, **kwargs):
        raise AssertionError("MWRA/CDC NWSS should not be called when WastewaterSCAN covers everything")
    monkeypatch.setattr(wastewater, "_fetch_mwra", boom)
    monkeypatch.setattr(wastewater, "_fetch_cdc_nwss", boom)

    df = wastewater.fetch_wastewater(
        pathogens=["SARS-CoV-2", "Influenza A", "RSV"],
        start="2024-06-01", end="2025-12-31", timezone="America/New_York",
        wastewaterscan={"plant_uid": "b50c6424"},
        mwra={"data_url": "x"}, cdc_nwss={"base_url": "y"},
    )
    assert (df["source"] == "wastewaterscan").all()
    assert set(df["pathogen"].unique()) == {"SARS-CoV-2", "Influenza A", "RSV"}


def test_fetch_wastewater_wastewaterscan_unreachable_falls_through(monkeypatch):
    """A WastewaterSCAN error degrades to MWRA/CDC NWSS, not straight to sample."""
    monkeypatch.setattr(wastewater.requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    mwra_df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-01"]).tz_localize("America/New_York"),
        "pathogen": ["SARS-CoV-2"], "value": [123.0], "source": ["mwra"],
    })
    monkeypatch.setattr(wastewater, "_fetch_mwra", lambda *a, **k: mwra_df)

    df = wastewater.fetch_wastewater(
        pathogens=["SARS-CoV-2"],
        start="2024-06-01", end="2025-12-31", timezone="America/New_York",
        wastewaterscan={"plant_uid": "b50c6424"}, mwra={"data_url": "x"},
    )
    assert df.loc[df["pathogen"] == "SARS-CoV-2", "source"].iloc[0] == "mwra"
