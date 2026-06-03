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
    # One year of daily timestamps, tz-naive (loaders localize on read).
    return pd.date_range("2024-06-01", "2025-05-31", freq="D")


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
    weeks = pd.date_range("2024-06-01", "2025-05-31", freq="W")
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
    make_hospital()
    make_events()
    print("Wrote sample data to data/samples/ and data/boston_events.csv")
    print("NOTE: this is SYNTHETIC data for testing the pipeline, not real observations.")


if __name__ == "__main__":
    main()
