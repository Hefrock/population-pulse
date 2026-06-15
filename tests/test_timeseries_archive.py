"""Tests for src/ingestion/timeseries_archive.py — in-place transit/weather accumulation."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from src.ingestion import timeseries_archive


def _transit_row(ts: str, route: str, value: float):
    return {"timestamp": pd.Timestamp(ts, tz="America/New_York"), "route": route, "value": value}


def _weather_row(ts: str, temperature_2m: float):
    return {"timestamp": pd.Timestamp(ts, tz="America/New_York"), "temperature_2m": temperature_2m}


# --- merge: transit (key = timestamp, route) ----------------------------------


def test_merge_appends_new_rows_to_empty_existing():
    existing = pd.DataFrame(columns=["timestamp", "route", "value"])
    new = pd.DataFrame([_transit_row("2025-01-01 00:00", "Red", 100)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp", "route"])

    assert len(out) == 1
    assert out.iloc[0]["route"] == "Red"
    assert out.iloc[0]["value"] == 100


def test_merge_dedupes_same_key_keeps_latest():
    """A re-fetched (timestamp, route) is refreshed in place, not duplicated."""
    existing = pd.DataFrame([_transit_row("2025-01-01 00:00", "Red", 100)])
    new = pd.DataFrame([_transit_row("2025-01-01 00:00", "Red", 150)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp", "route"])

    assert len(out) == 1
    assert out.iloc[0]["value"] == 150


def test_merge_keeps_distinct_routes_at_same_timestamp():
    existing = pd.DataFrame([_transit_row("2025-01-01 00:00", "Red", 100)])
    new = pd.DataFrame([_transit_row("2025-01-01 00:00", "Green", 75)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp", "route"])

    assert len(out) == 2
    assert set(out["route"]) == {"Red", "Green"}


def test_merge_preserves_existing_rows_outside_new_window():
    """The core accumulation behavior: old history not covered by the new
    fetch's window survives the merge."""
    existing = pd.DataFrame([_transit_row("2020-01-01 00:00", "Red", 50)])
    new = pd.DataFrame([_transit_row("2025-01-01 00:00", "Red", 100)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp", "route"])

    assert len(out) == 2
    assert set(out["timestamp"]) == {
        pd.Timestamp("2020-01-01 00:00", tz="America/New_York"),
        pd.Timestamp("2025-01-01 00:00", tz="America/New_York"),
    }


def test_merge_sorts_by_timestamp():
    existing = pd.DataFrame([_transit_row("2025-02-01 00:00", "Red", 100)])
    new = pd.DataFrame([_transit_row("2025-01-01 00:00", "Red", 50)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp", "route"])

    assert list(out["timestamp"]) == [
        pd.Timestamp("2025-01-01 00:00", tz="America/New_York"),
        pd.Timestamp("2025-02-01 00:00", tz="America/New_York"),
    ]


# --- merge: weather (key = timestamp, wide form) -------------------------------


def test_merge_weather_dedupes_on_timestamp_only():
    existing = pd.DataFrame([_weather_row("2025-01-01 00:00", 32.0)])
    new = pd.DataFrame([_weather_row("2025-01-01 00:00", 35.5)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp"])

    assert len(out) == 1
    assert out.iloc[0]["temperature_2m"] == 35.5


def test_merge_empty_new_keeps_existing():
    """A failed fetch (empty frame) doesn't wipe out accumulated history."""
    existing = pd.DataFrame([_weather_row("2025-01-01 00:00", 32.0)])
    new = pd.DataFrame(columns=["timestamp", "temperature_2m"])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp"])

    assert len(out) == 1
    assert out.iloc[0]["temperature_2m"] == 32.0


# --- load_existing ---------------------------------------------------------------


def test_load_existing_reads_local_file(tmp_path):
    local_path = tmp_path / "transit.parquet"
    df = pd.DataFrame([_transit_row("2025-01-01 00:00", "Red", 100)])
    df.to_parquet(local_path, index=False)

    out = timeseries_archive.load_existing(
        local_path, archive_url="https://example.invalid/never-used", columns=["timestamp", "route", "value"]
    )

    assert len(out) == 1
    assert out.iloc[0]["route"] == "Red"


def test_load_existing_falls_back_to_remote_url(tmp_path, monkeypatch):
    local_path = tmp_path / "transit.parquet"  # does not exist

    df = pd.DataFrame([_transit_row("2025-01-01 00:00", "Red", 100)])
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)

    class _FakeResp:
        content = buf.getvalue()

        def raise_for_status(self):
            pass

    monkeypatch.setattr(timeseries_archive.requests, "get", lambda *a, **k: _FakeResp())

    out = timeseries_archive.load_existing(
        local_path,
        archive_url="https://example.invalid/data/transit.parquet",
        columns=["timestamp", "route", "value"],
    )

    assert len(out) == 1
    assert out.iloc[0]["route"] == "Red"


def test_load_existing_returns_empty_frame_when_unreachable(tmp_path, monkeypatch):
    local_path = tmp_path / "transit.parquet"  # does not exist

    def boom(*a, **k):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(timeseries_archive.requests, "get", boom)

    out = timeseries_archive.load_existing(
        local_path,
        archive_url="https://example.invalid/data/transit.parquet",
        columns=["timestamp", "route", "value"],
    )

    assert out.empty
    assert list(out.columns) == ["timestamp", "route", "value"]
