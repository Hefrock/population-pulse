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


def _wastewater_row(ts: str, pathogen: str, value: float):
    return {"timestamp": pd.Timestamp(ts, tz="America/New_York"), "pathogen": pathogen, "value": value}


def _hospital_row(ts: str, metric: str, value: float):
    return {"timestamp": pd.Timestamp(ts, tz="America/New_York"), "metric": metric, "value": value}


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


# --- merge: wastewater (key = timestamp, pathogen) -----------------------------


def test_merge_wastewater_preserves_history_outside_narrow_refetch_window():
    """The bug this guards against: wastewater/hospital_demand/academic_calendar
    used to be overwritten outright instead of merged, so the daily cron's
    default trailing-365-day window silently erased older accumulated history
    on every run. A signal merged on (timestamp, pathogen) must keep rows
    outside a later narrow re-fetch's window."""
    existing = pd.DataFrame([_wastewater_row("2022-12-12 00:00", "Influenza A", 10.0)])
    new = pd.DataFrame([_wastewater_row("2026-06-01 00:00", "Influenza A", 20.0)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp", "pathogen"])

    assert len(out) == 2
    assert set(out["timestamp"]) == {
        pd.Timestamp("2022-12-12 00:00", tz="America/New_York"),
        pd.Timestamp("2026-06-01 00:00", tz="America/New_York"),
    }


def test_merge_wastewater_keeps_distinct_pathogens_at_same_timestamp():
    existing = pd.DataFrame([_wastewater_row("2025-01-01 00:00", "Influenza A", 10.0)])
    new = pd.DataFrame([_wastewater_row("2025-01-01 00:00", "RSV", 5.0)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp", "pathogen"])

    assert len(out) == 2
    assert set(out["pathogen"]) == {"Influenza A", "RSV"}


def test_merge_wastewater_refetch_overwrites_revised_value():
    """A re-fetched date is refreshed to the newer value -- deliberate, since
    upstream wastewater surveillance values get revised after publication and
    the pipeline should absorb the correction rather than stay stale forever."""
    existing = pd.DataFrame([_wastewater_row("2025-01-01 00:00", "Influenza A", 10.0)])
    new = pd.DataFrame([_wastewater_row("2025-01-01 00:00", "Influenza A", 12.5)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp", "pathogen"])

    assert len(out) == 1
    assert out.iloc[0]["value"] == 12.5


# --- merge: hospital_demand (key = timestamp, metric) ---------------------------


def test_merge_hospital_demand_preserves_history_outside_narrow_refetch_window():
    existing = pd.DataFrame([_hospital_row("2019-06-30 00:00", "ed_visits_respiratory", 100.0)])
    new = pd.DataFrame([_hospital_row("2026-06-01 00:00", "ed_visits_respiratory", 150.0)])

    out = timeseries_archive.merge(existing, new, key_columns=["timestamp", "metric"])

    assert len(out) == 2
    assert set(out["timestamp"]) == {
        pd.Timestamp("2019-06-30 00:00", tz="America/New_York"),
        pd.Timestamp("2026-06-01 00:00", tz="America/New_York"),
    }


# --- run.py wiring -----------------------------------------------------------


def test_run_timeseries_key_columns_covers_every_self_archiving_signal():
    """Locks in the fix: every signal whose fetcher already returns real
    historical data (not just events, which has its own archive mechanism)
    must accumulate via merge, or the daily cron's default narrow window
    erodes it back down to ~365 days on its very next run."""
    from src.ingestion import run

    assert run.TIMESERIES_KEY_COLUMNS == {
        "transit": ["timestamp", "route"],
        "weather": ["timestamp"],
        "bikeshare": ["timestamp"],
        "academic_calendar": ["timestamp", "school"],
        "wastewater": ["timestamp", "pathogen"],
        "hospital_demand": ["timestamp", "metric"],
    }


def test_check_archiving_coverage_passes_for_known_signals():
    from src.ingestion import run

    run._check_archiving_coverage(set(run.TIMESERIES_KEY_COLUMNS) | run.NOT_ACCUMULATED)


def test_check_archiving_coverage_raises_for_a_new_unwired_signal():
    """Guards against the exact bug class found in this session: a new
    fetcher gets added to run()'s ``signals`` dict but nobody updates
    TIMESERIES_KEY_COLUMNS, so it's silently overwritten by the next run."""
    from src.ingestion import run

    with pytest.raises(RuntimeError, match="ambulance"):
        run._check_archiving_coverage(set(run.TIMESERIES_KEY_COLUMNS) | {"ambulance"})


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
