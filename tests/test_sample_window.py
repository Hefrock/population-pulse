"""Tests for the shared sample-window shift helper."""

from __future__ import annotations

import pandas as pd

from src.ingestion.sample_window import shift_sample_to_window


def _sample(start: str, end: str, freq: str = "W") -> pd.DataFrame:
    return pd.DataFrame({"timestamp": pd.date_range(start, end, freq=freq)})


def test_no_shift_when_already_overlapping():
    df = _sample("2025-01-01", "2025-12-31")
    out = shift_sample_to_window(df, "2025-06-01", "2025-07-01")
    pd.testing.assert_frame_equal(out, df)


def test_shifts_forward_when_sample_is_older_than_window():
    df = _sample("2025-01-01", "2025-12-31")
    out = shift_sample_to_window(df, "2028-06-01", "2028-07-01")
    assert out["timestamp"].max() >= pd.Timestamp("2028-06-01")
    assert out["timestamp"].min() <= pd.Timestamp("2028-07-01")
    # whole-year shift preserves calendar month/day — the seasonal position of
    # each row — even across a leap year (where day-of-year would drift by one).
    assert (list(zip(out["timestamp"].dt.month, out["timestamp"].dt.day))
            == list(zip(df["timestamp"].dt.month, df["timestamp"].dt.day)))


def test_shifts_backward_when_sample_is_newer_than_window():
    df = _sample("2030-01-01", "2030-12-31")
    out = shift_sample_to_window(df, "2026-06-01", "2026-07-01")
    assert out["timestamp"].min() <= pd.Timestamp("2026-07-01")
    assert out["timestamp"].max() >= pd.Timestamp("2026-06-01")


def test_empty_frame_is_returned_unchanged():
    df = pd.DataFrame({"timestamp": pd.to_datetime([])})
    out = shift_sample_to_window(df, "2025-01-01", "2025-02-01")
    assert out.empty


def test_custom_timestamp_column():
    df = pd.DataFrame({"when": pd.date_range("2025-01-01", "2025-12-31", freq="W")})
    out = shift_sample_to_window(df, "2028-01-01", "2028-12-31", timestamp_col="when")
    assert out["when"].max().year >= 2028
