"""Tests for the Bluebikes (bike-share) fetcher."""

from __future__ import annotations

import io
import zipfile

import pandas as pd
import pytest

from src.analysis.correlate import align
from src.ingestion import bluebikes


class _FakeResp:
    def __init__(self, content: bytes, status_code: int = 200, payload=None):
        self.content = content
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _zip_csv(filename: str, csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, csv_text)
    return buf.getvalue()


# --- _month_range -------------------------------------------------------------

def test_month_range_covers_each_month_inclusive():
    assert bluebikes._month_range("2025-06-01", "2025-08-15") == ["202506", "202507", "202508"]


def test_month_range_single_month():
    assert bluebikes._month_range("2025-06-05", "2025-06-20") == ["202506"]


# --- _detect_start_column ------------------------------------------------------

def test_detect_start_column_new_format():
    cols = ["ride_id", "rideable_type", "started_at", "ended_at"]
    assert bluebikes._detect_start_column(cols) == "started_at"


def test_detect_start_column_old_format():
    cols = ["tripduration", "starttime", "stoptime", "bikeid"]
    assert bluebikes._detect_start_column(cols) == "starttime"


def test_detect_start_column_missing():
    assert bluebikes._detect_start_column(["foo", "bar"]) is None


# --- fetch_trip_history ---------------------------------------------------------

def test_fetch_trip_history_aggregates_by_day(monkeypatch):
    csv = (
        "ride_id,started_at,ended_at\n"
        "1,2025-06-15 08:00:00,2025-06-15 08:20:00\n"
        "2,2025-06-15 09:00:00,2025-06-15 09:25:00\n"
        "3,2025-06-16 08:00:00,2025-06-16 08:15:00\n"
    )
    zipped = _zip_csv("202506-bluebikes-tripdata.csv", csv)

    def fake_get(url, timeout=None):
        assert url == "https://s3.amazonaws.com/hubway-data/202506-bluebikes-tripdata.zip"
        return _FakeResp(zipped)

    monkeypatch.setattr(bluebikes.requests, "get", fake_get)

    df = bluebikes.fetch_trip_history(
        base_url="https://s3.amazonaws.com/hubway-data",
        start="2025-06-01", end="2025-06-30",
        timezone="America/New_York",
    )
    assert list(df.columns) == ["timestamp", "value"]
    assert str(df["timestamp"].dt.tz) == "America/New_York"
    by_day = dict(zip(df["timestamp"].dt.date.astype(str), df["value"]))
    assert by_day == {"2025-06-15": 2, "2025-06-16": 1}


def test_fetch_trip_history_filters_to_window(monkeypatch):
    csv = (
        "ride_id,started_at,ended_at\n"
        "1,2025-05-31 23:00:00,2025-06-01 00:10:00\n"  # outside window -> dropped
        "2,2025-06-10 08:00:00,2025-06-10 08:15:00\n"
    )
    zipped = _zip_csv("202506-bluebikes-tripdata.csv", csv)
    monkeypatch.setattr(bluebikes.requests, "get", lambda url, timeout=None: _FakeResp(zipped))

    df = bluebikes.fetch_trip_history(
        base_url="https://s3.amazonaws.com/hubway-data",
        start="2025-06-01", end="2025-06-30",
        timezone="America/New_York",
    )
    assert len(df) == 1
    assert df["value"].iloc[0] == 1
    assert df["timestamp"].dt.date.astype(str).iloc[0] == "2025-06-10"


def test_fetch_trip_history_skips_unavailable_months(monkeypatch):
    """A 404/exception for one month's file shouldn't kill the others."""
    csv_june = "ride_id,started_at\n1,2025-06-15 08:00:00\n"

    def fake_get(url, timeout=None):
        if "202506" in url:
            return _FakeResp(_zip_csv("202506-bluebikes-tripdata.csv", csv_june))
        return _FakeResp(b"", status_code=404)

    monkeypatch.setattr(bluebikes.requests, "get", fake_get)

    df = bluebikes.fetch_trip_history(
        base_url="https://s3.amazonaws.com/hubway-data",
        start="2025-06-01", end="2025-07-31",
        timezone="America/New_York",
    )
    assert len(df) == 1
    assert df["timestamp"].dt.date.astype(str).iloc[0] == "2025-06-15"


def test_fetch_trip_history_empty_when_nothing_available(monkeypatch):
    monkeypatch.setattr(
        bluebikes.requests, "get", lambda url, timeout=None: _FakeResp(b"", status_code=404)
    )
    df = bluebikes.fetch_trip_history(
        base_url="https://s3.amazonaws.com/hubway-data",
        start="2025-06-01", end="2025-06-30",
        timezone="America/New_York",
    )
    assert list(df.columns) == ["timestamp", "value"]
    assert df.empty


# --- fetch_station_status (GBFS) ------------------------------------------------

def test_fetch_station_status_sums_bikes_available(monkeypatch):
    payload = {"data": {"stations": [
        {"station_id": "a", "num_bikes_available": 10},
        {"station_id": "b", "num_bikes_available": 25},
    ]}}
    monkeypatch.setattr(
        bluebikes.requests, "get",
        lambda url, timeout=None: _FakeResp(b"", payload=payload),
    )

    df = bluebikes.fetch_station_status(base_url=None, timezone="America/New_York")
    assert list(df.columns) == ["timestamp", "value"]
    assert len(df) == 1
    assert df["value"].iloc[0] == 35
    assert str(df["timestamp"].dt.tz) == "America/New_York"


def test_fetch_station_status_empty_when_no_stations(monkeypatch):
    payload = {"data": {"stations": []}}
    monkeypatch.setattr(
        bluebikes.requests, "get",
        lambda url, timeout=None: _FakeResp(b"", payload=payload),
    )
    df = bluebikes.fetch_station_status(base_url=None, timezone="America/New_York")
    assert df.empty


# --- fetch_bikeshare tiering -----------------------------------------------------

def test_fetch_bikeshare_falls_back_to_gbfs_when_trip_history_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    payload = {"data": {"stations": [{"station_id": "a", "num_bikes_available": 7}]}}

    def fake_get(url, timeout=None):
        if "hubway-data" in url:
            raise RuntimeError("network down")
        return _FakeResp(b"", payload=payload)

    monkeypatch.setattr(bluebikes.requests, "get", fake_get)

    df = bluebikes.fetch_bikeshare(
        start="2025-06-01", end="2025-06-30", timezone="America/New_York",
        trip_history={"base_url": "https://s3.amazonaws.com/hubway-data"},
        gbfs={"base_url": "https://gbfs.bluebikes.com/gbfs"},
    )
    assert list(df.columns) == ["timestamp", "value"]
    assert df["value"].iloc[0] == 7


def test_fetch_bikeshare_falls_back_to_sample_when_all_sources_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bluebikes.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")),
    )

    sample = tmp_path / "bluebikes_sample.csv"
    pd.DataFrame({
        "timestamp": ["2025-06-10", "2025-06-11"],
        "value": [1000, 1100],
    }).to_csv(sample, index=False)
    monkeypatch.setattr(bluebikes, "SAMPLE_PATH", sample)

    df = bluebikes.fetch_bikeshare(
        start="2025-06-01", end="2025-06-30", timezone="America/New_York",
        trip_history={"base_url": "https://s3.amazonaws.com/hubway-data"},
        gbfs={"base_url": "https://gbfs.bluebikes.com/gbfs"},
    )
    assert not df.empty
    assert list(df.columns) == ["timestamp", "value"]
    assert str(df["timestamp"].dt.tz) == "America/New_York"


def test_fetch_bikeshare_no_config_uses_sample(monkeypatch, tmp_path):
    sample = tmp_path / "bluebikes_sample.csv"
    pd.DataFrame({
        "timestamp": ["2025-06-10"],
        "value": [1000],
    }).to_csv(sample, index=False)
    monkeypatch.setattr(bluebikes, "SAMPLE_PATH", sample)

    df = bluebikes.fetch_bikeshare(
        start="2025-06-01", end="2025-06-30", timezone="America/New_York",
        trip_history=None, gbfs=None,
    )
    assert len(df) == 1


# --- align() interaction ---------------------------------------------------

def test_bikeshare_output_aligns():
    """fetch_trip_history's timestamp/value shape resamples cleanly."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-06-01", periods=14, freq="D", tz="America/New_York"),
        "value": [100.0] * 14,
    })
    result = align({"bikeshare": df})
    assert "bikeshare" in result.columns
    assert result["bikeshare"].dropna().sum() == pytest.approx(1400.0)
