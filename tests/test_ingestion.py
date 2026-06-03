"""Tests for ingestion fetcher edge cases."""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from src.ingestion.cdc_fluview import _date_to_epiweek, _epiweek_to_timestamp
from src.ingestion.ticketmaster import _empty_frame as tm_empty
from src.ingestion.eventbrite import _empty_frame as eb_empty
from src.ingestion import mbta


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


def test_eventbrite_empty_frame_schema():
    df = eb_empty()
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


def test_eventbrite_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv("EVENTBRITE_API_KEY", raising=False)
    from src.ingestion.eventbrite import fetch_events
    df = fetch_events(
        base_url="https://www.eventbriteapi.com/v3",
        location="Boston, MA, USA", radius="25mi",
        start="2025-01-01", end="2025-03-31",
        timezone="America/New_York",
    )
    assert df.empty


# --- MBTA historical gated-entries ------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _arcgis_router(monkeypatch, *, fields, features):
    """Patch mbta.requests.get to emulate the 3-call ArcGIS conversation."""
    def fake_get(url, params=None, timeout=None):
        if "/sharing/rest/content/items/" in url:
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
