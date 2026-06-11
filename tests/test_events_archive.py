"""Tests for src/ingestion/events_archive.py — the accumulating events history."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from src.ingestion import events_archive


def _row(date: str, venue: str, name: str, attendance=None, source="ticketmaster"):
    return {
        "timestamp": pd.Timestamp(date, tz="America/New_York"),
        "venue": venue,
        "name": name,
        "expected_attendance": attendance,
        "source": source,
    }


# --- merge -------------------------------------------------------------------


def test_merge_appends_new_events_to_empty_archive():
    existing = pd.DataFrame(columns=events_archive.ARCHIVE_COLUMNS)
    new = pd.DataFrame([_row("2026-07-04", "Boston Common", "Fourth of July")])

    out = events_archive.merge(existing, new)

    assert len(out) == 1
    assert out.iloc[0]["name"] == "Fourth of July"


def test_merge_dedupes_same_event_keeps_latest_snapshot():
    """A re-fetched event (same date + name) is updated in place, not duplicated."""
    existing = pd.DataFrame([_row("2026-07-04", "Boston Common", "Fourth of July", attendance=None)])
    new = pd.DataFrame([_row("2026-07-04", "Boston Common", "Fourth of July", attendance=50000)])

    out = events_archive.merge(existing, new)

    assert len(out) == 1
    assert out.iloc[0]["expected_attendance"] == 50000


def test_merge_dedup_is_case_insensitive_on_name():
    existing = pd.DataFrame([_row("2026-07-04", "Boston Common", "fourth of july")])
    new = pd.DataFrame([_row("2026-07-04", "Boston Common", "Fourth Of July", attendance=50000)])

    out = events_archive.merge(existing, new)

    assert len(out) == 1
    assert out.iloc[0]["expected_attendance"] == 50000


def test_merge_keeps_distinct_events_on_same_day():
    existing = pd.DataFrame([_row("2026-07-04", "Boston Common", "Fourth of July")])
    new = pd.DataFrame([_row("2026-07-04", "TD Garden", "Celtics Game")])

    out = events_archive.merge(existing, new)

    assert len(out) == 2
    assert set(out["name"]) == {"Fourth of July", "Celtics Game"}


def test_merge_sorts_by_timestamp():
    existing = pd.DataFrame([_row("2026-12-31", "Boston Common", "First Night")])
    new = pd.DataFrame([_row("2026-07-04", "Boston Common", "Fourth of July")])

    out = events_archive.merge(existing, new)

    assert list(out["name"]) == ["Fourth of July", "First Night"]


def test_merge_fills_missing_columns_on_new_frame():
    """A new frame missing optional columns (e.g. no 'source') doesn't blow up."""
    existing = pd.DataFrame(columns=events_archive.ARCHIVE_COLUMNS)
    new = pd.DataFrame([{
        "timestamp": pd.Timestamp("2026-07-04", tz="America/New_York"),
        "venue": "Boston Common",
        "name": "Fourth of July",
        "expected_attendance": None,
    }])

    out = events_archive.merge(existing, new)

    assert len(out) == 1
    assert list(out.columns) == events_archive.ARCHIVE_COLUMNS


# --- load_existing -------------------------------------------------------------


def test_load_existing_reads_local_file(tmp_path):
    local_path = tmp_path / "events_archive.parquet"
    df = pd.DataFrame([_row("2026-07-04", "Boston Common", "Fourth of July")])
    df.to_parquet(local_path, index=False)

    out = events_archive.load_existing(local_path, archive_url="https://example.invalid/never-used")

    assert len(out) == 1
    assert out.iloc[0]["name"] == "Fourth of July"


def test_load_existing_falls_back_to_remote_url(tmp_path, monkeypatch):
    local_path = tmp_path / "events_archive.parquet"  # does not exist

    df = pd.DataFrame([_row("2026-07-04", "Boston Common", "Fourth of July")])
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)

    class _FakeResp:
        content = buf.getvalue()

        def raise_for_status(self):
            pass

    monkeypatch.setattr(events_archive.requests, "get", lambda *a, **k: _FakeResp())

    out = events_archive.load_existing(local_path, archive_url="https://example.invalid/data/events_archive.parquet")

    assert len(out) == 1
    assert out.iloc[0]["name"] == "Fourth of July"


def test_load_existing_returns_empty_frame_when_unreachable(tmp_path, monkeypatch):
    local_path = tmp_path / "events_archive.parquet"  # does not exist

    def boom(*a, **k):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(events_archive.requests, "get", boom)

    out = events_archive.load_existing(local_path, archive_url="https://example.invalid/data/events_archive.parquet")

    assert out.empty
    assert list(out.columns) == events_archive.ARCHIVE_COLUMNS
