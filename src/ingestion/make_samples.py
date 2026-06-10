"""Generate synthetic sample data so the whole pipeline runs offline.

This produces *plausible-looking but fake* data with a deliberately planted
signal: a winter respiratory surge, a summer heat spike, and a couple of large
events. That lets you verify the alignment + correlation code actually detects
known relationships before you trust it on real data.

Run once:
    python -m src.ingestion.make_samples
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SAMPLES = Path("data/samples")
RNG = np.random.default_rng(42)


def _date_range() -> pd.DatetimeIndex:
    """Trailing ~365 days of daily timestamps, tz-naive (loaders localize on read).

    Anchored to *today* rather than a fixed calendar window — run.py's default
    ingestion window is also "trailing 365 days from today", and a fixed window
    silently drifts out of that range over time. When that happens, any signal
    that falls back to its sample (e.g. wastewater when MWRA/CDC are unreachable)
    gets filtered down to zero rows with no error — exactly what happened here.
    """
    end = pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=364)
    return pd.date_range(start, end, freq="D")


def _week_range() -> pd.DatetimeIndex:
    days = _date_range()
    return pd.date_range(days[0], days[-1], freq="W")


def make_transit() -> None:
    """Daily ridership proxy: weekly commute cycle + mild winter dip + noise."""
    days = _date_range()
    routes = ["Red", "Orange", "Blue", "Green-B"]
    rows = []
    for route in routes:
        base = {"Red": 600, "Orange": 450, "Blue": 300, "Green-B": 250}[route]
        for d in days:
            weekday_factor = 0.6 if d.dayofweek >= 5 else 1.0  # weekends lower
            seasonal = 1.0 - 0.08 * np.cos(2 * np.pi * (d.dayofyear / 365))
            value = base * weekday_factor * seasonal * RNG.normal(1.0, 0.05)
            rows.append({"timestamp": d, "route": route, "value": round(value)})
    pd.DataFrame(rows).to_csv(SAMPLES / "mbta_ridership_sample.csv", index=False)


def make_hospital() -> None:
    """Weekly ED respiratory visits with a planted winter surge."""
    weeks = _week_range()
    rows = []
    for w in weeks:
        # Surge centered on January (~day 15 of year).
        doy = w.dayofyear
        winter = 200 + 180 * np.exp(-((doy - 15) ** 2) / (2 * 40 ** 2))
        winter += 180 * np.exp(-((doy - 380) ** 2) / (2 * 40 ** 2))  # wrap to Dec
        rows.append(
            {
                "timestamp": w,
                "metric": "ed_visits_respiratory",
                "value": round(winter * RNG.normal(1.0, 0.06)),
            }
        )
    pd.DataFrame(rows).to_csv(SAMPLES / "hospital_demand_sample.csv", index=False)


def make_wastewater() -> None:
    """Weekly multi-pathogen wastewater levels that LEAD the hospital surge.

    Wastewater is a leading indicator, so each pathogen's surge is planted a
    couple of weeks *earlier* than the hospital winter peak (day-of-year ~15).
    That lets the lagged cross-correlation recover a positive lead lag — the
    whole point of including this signal. Each virus also gets its own seasonal
    shape: RSV peaks earliest (late fall), flu sharpest mid-winter, SARS-CoV-2
    broad winter plus a smaller summer bump.
    """
    weeks = _week_range()
    # Shapes keyed by (winter-peak day-of-year, width, summer-bump amplitude).
    shapes = {
        "SARS-CoV-2": (1, 45, 0.5),   # broad, leads hospital peak; summer wave
        "Influenza A": (1, 25, 0.0),  # sharp mid-winter, no summer activity
        "RSV": (-20, 30, 0.0),        # peaks earliest, late fall / early winter
    }
    rows = []
    for pathogen, (peak_doy, width, summer) in shapes.items():
        for w in weeks:
            doy = w.dayofyear
            # Winter peak (with December wrap-around), planted ahead of hospital.
            level = 2.0 + 6.0 * np.exp(-((doy - peak_doy) ** 2) / (2 * width ** 2))
            level += 6.0 * np.exp(-((doy - (365 + peak_doy)) ** 2) / (2 * width ** 2))
            if summer:
                level += summer * 6.0 * np.exp(-((doy - 200) ** 2) / (2 * 30 ** 2))
            rows.append(
                {
                    "timestamp": w,
                    "pathogen": pathogen,
                    "value": round(level * RNG.normal(1.0, 0.05), 2),
                    "source": "sample",
                }
            )
    pd.DataFrame(rows).to_csv(SAMPLES / "wastewater_sample.csv", index=False)


def make_weather() -> None:
    """Hourly temperature/apparent-temperature/precipitation with planted
    seasonal extremes — a winter cold snap and a summer heat spike — mirroring
    the heat-stress / cold / asthma drivers in the weather sub-hypothesis.
    """
    days = _date_range()
    hours = pd.date_range(days[0], days[-1] + pd.Timedelta(days=1), freq="h", inclusive="left")
    rows = []
    for h in hours:
        doy = h.dayofyear
        seasonal = 10 - 14 * np.cos(2 * np.pi * (doy - 15) / 365)
        diurnal = 4 * np.sin(2 * np.pi * (h.hour - 9) / 24)
        temp = seasonal + diurnal + RNG.normal(0, 1.5)
        # Apparent temperature exaggerates extremes: wind chill below 5C,
        # heat index above 25C.
        if temp < 5:
            apparent = temp - 3 + RNG.normal(0, 1.0)
        elif temp > 25:
            apparent = temp + 3 + RNG.normal(0, 1.0)
        else:
            apparent = temp + RNG.normal(0, 1.0)
        precip = round(max(0.0, RNG.normal(0, 0.3)), 2) if RNG.random() < 0.12 else 0.0
        rows.append(
            {
                "timestamp": h,
                "temperature_2m": round(temp, 1),
                "apparent_temperature": round(apparent, 1),
                "precipitation": precip,
            }
        )
    pd.DataFrame(rows).to_csv(SAMPLES / "weather_sample.csv", index=False)


def make_events() -> None:
    """A handful of clean, datable large gatherings."""
    rows = [
        {"date": "2024-10-15", "venue": "TD Garden", "name": "Celtics Opener", "expected_attendance": 19000},
        {"date": "2024-12-31", "venue": "Boston Common", "name": "First Night", "expected_attendance": 50000},
        {"date": "2025-04-21", "venue": "Boston", "name": "Boston Marathon", "expected_attendance": 500000},
    ]
    pd.DataFrame(rows).to_csv(Path("data/boston_events.csv"), index=False)


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    Path("data").mkdir(exist_ok=True)
    make_transit()
    make_weather()
    make_hospital()
    make_wastewater()
    make_events()
    print("Wrote sample data to data/samples/ and data/boston_events.csv")
    print("NOTE: this is SYNTHETIC data for testing the pipeline, not real observations.")


if __name__ == "__main__":
    main()
