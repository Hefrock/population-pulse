"""Tests for the hospital-demand fetcher's three-tier fallback."""

from __future__ import annotations

import datetime

import pandas as pd

from src.ingestion import hospital


def _write_csv(path, rows):
    pd.DataFrame(rows, columns=["timestamp", "metric", "value"]).to_csv(path, index=False)


def test_load_csv_filters_by_date_and_metric(tmp_path):
    csv_path = tmp_path / "ma_dph.csv"
    _write_csv(csv_path, [
        ("2025-01-05", "ed_visits_respiratory", 100),
        ("2025-01-05", "hospital_admissions_respiratory", 10),
        ("2025-06-01", "ed_visits_respiratory", 200),  # outside requested range
    ])

    df = hospital._load_csv(
        csv_path, ["ed_visits_respiratory"],
        start="2025-01-01", end="2025-02-01", timezone="America/New_York",
    )

    assert df["metric"].tolist() == ["ed_visits_respiratory"]
    assert df["value"].tolist() == [100]
    assert df["timestamp"].dt.tz is not None


def test_fetch_uses_ma_dph_csv_when_present(monkeypatch, tmp_path):
    csv_path = tmp_path / "ma_dph.csv"
    _write_csv(csv_path, [
        ("2025-01-05", "ed_visits_respiratory", 100),
        ("2025-01-05", "hospital_admissions_respiratory", 10),
    ])
    monkeypatch.setattr(hospital, "CACHED_PATH", csv_path)

    df = hospital.fetch_ma_dph_respiratory(
        metrics=["ed_visits_respiratory", "hospital_admissions_respiratory"],
        start="2025-01-01", end="2025-02-01", timezone="America/New_York",
    )

    assert set(df["metric"]) == {"ed_visits_respiratory", "hospital_admissions_respiratory"}


def test_fetch_falls_back_to_cdc_fluview_when_no_cached_csv(monkeypatch, tmp_path):
    monkeypatch.setattr(hospital, "CACHED_PATH", tmp_path / "missing.csv")

    fake_ili = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-05"]).tz_localize("America/New_York"),
        "metric": ["ili_patients"],
        "value": [42.0],
    })
    monkeypatch.setattr(hospital.cdc_fluview, "fetch_ili_data", lambda **kw: fake_ili)

    df = hospital.fetch_ma_dph_respiratory(
        metrics=["ili_patients"], start="2025-01-01", end="2025-02-01", timezone="America/New_York",
    )

    assert df["metric"].tolist() == ["ili_patients"]


def test_fetch_falls_back_to_sample_when_nothing_else_available(monkeypatch, tmp_path):
    monkeypatch.setattr(hospital, "CACHED_PATH", tmp_path / "missing.csv")
    monkeypatch.setattr(hospital.cdc_fluview, "fetch_ili_data", lambda **kw: hospital.cdc_fluview._empty_frame())

    df = hospital.fetch_ma_dph_respiratory(
        metrics=["ed_visits_respiratory"], start="2000-01-01", end="2030-01-01", timezone="America/New_York",
    )

    assert (df["metric"] == "ed_visits_respiratory").all()
    assert not df.empty


def test_sample_fallback_covers_window_past_its_own_dates(monkeypatch, tmp_path):
    """The synthetic sample tier must stay non-empty for a today-relative window
    that has aged past the frozen sample (shifted by whole years), the same
    guard weather and wastewater have."""
    monkeypatch.setattr(hospital, "CACHED_PATH", tmp_path / "missing.csv")
    monkeypatch.setattr(hospital.cdc_fluview, "fetch_ili_data", lambda **kw: hospital.cdc_fluview._empty_frame())

    future_start = (datetime.date.today() + datetime.timedelta(days=365 * 3)).isoformat()
    future_end = (datetime.date.today() + datetime.timedelta(days=365 * 3 + 120)).isoformat()
    df = hospital.fetch_ma_dph_respiratory(
        metrics=["ed_visits_respiratory"], start=future_start, end=future_end, timezone="America/New_York",
    )

    assert not df.empty
    assert (df["metric"] == "ed_visits_respiratory").all()
    assert df["timestamp"].min() >= pd.Timestamp(future_start, tz="America/New_York")


def test_load_csv_does_not_shift_real_data(tmp_path):
    """The shift is sample-tier only: real MA DPH data (shift_to_window=False,
    the default) must still be clipped to the window, never slid into range."""
    csv_path = tmp_path / "ma_dph.csv"
    _write_csv(csv_path, [
        ("2025-01-05", "ed_visits_respiratory", 100),
        ("2025-06-01", "ed_visits_respiratory", 200),  # outside requested range
    ])
    df = hospital._load_csv(
        csv_path, ["ed_visits_respiratory"],
        start="2025-01-01", end="2025-02-01", timezone="America/New_York",
    )
    assert df["value"].tolist() == [100]


def test_provisional_cutoff_is_n_weeks_before_latest():
    latest = pd.Timestamp("2026-07-12", tz="America/New_York")
    cutoff = hospital.provisional_cutoff(latest)
    assert cutoff == latest - pd.Timedelta(weeks=hospital.PROVISIONAL_WEEKS)
    # the latest week itself must always fall after the cutoff (i.e. be provisional)
    assert latest > cutoff
