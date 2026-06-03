"""Tests for ingestion fetcher edge cases."""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from src.ingestion.cdc_fluview import _date_to_epiweek, _epiweek_to_timestamp
from src.ingestion.ticketmaster import _empty_frame as tm_empty
from src.ingestion.eventbrite import _empty_frame as eb_empty


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
