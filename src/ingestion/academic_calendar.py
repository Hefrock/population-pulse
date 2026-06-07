"""Academic-calendar population-driver fetcher.

Boston's population swells by roughly 150,000 students each fall and spring
semester — a population-flow driver at least as large as any single sporting
event, and one with a textbook epidemiological link ("school comes back into
session -> ILI ticks up a couple weeks later" is one of the most reliable
seasonal patterns in surveillance data). It's currently absent from the
pipeline's events/weather/transit trio.

There is no API for term dates: schools publish them as PDFs or HTML pages
that change layout every year, so scraping them would be the most fragile
fetcher in the pipeline. Instead this is a hand-curated reference CSV
(``data/boston_academic_calendar.csv``, columns: school, enrollment, term,
start_date, end_date) — the same "manual baseline" pattern already used for
``boston_events.csv``. It needs a ~10-minute refresh once a year when each
school publishes its next academic calendar.

For each school+term we build a daily 0..1 "in session" weight that ramps
linearly over RAMP_DAYS at move-in and move-out, rather than stepping
abruptly — that better matches how a real population surge arrives and
departs, and avoids an artificial one-day spike/cliff in the aligned series.
``enrollment * weight`` estimates how many of that school's students are
physically in the city on a given day; ``align()`` then sums the per-school
rows into one composite "students in city" index, exactly as it already sums
MBTA's per-route ridership into a single transit signal.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

RAMP_DAYS = 7


def fetch_population_index(
    path: str | Path,
    start: str,
    end: str,
    timezone: str,
    ramp_days: int = RAMP_DAYS,
) -> pd.DataFrame:
    """Return a daily per-school "students present" series.

    Columns: ``timestamp``, ``school``, ``value`` (enrollment x in-session
    weight). Missing file returns an empty (correctly-typed) frame so the
    pipeline keeps running — this signal is optional, like manual events.
    """
    cols = ["timestamp", "school", "value"]
    csv_path = Path(path)
    if not csv_path.exists():
        print(f"[academic_calendar] No calendar file at {csv_path}; skipping population-driver signal.")
        return pd.DataFrame(columns=cols)

    terms = pd.read_csv(csv_path, parse_dates=["start_date", "end_date"])
    if terms.empty:
        return pd.DataFrame(columns=cols)

    days = pd.date_range(start, end, freq="D", tz=timezone)
    if days.empty:
        return pd.DataFrame(columns=cols)

    rows = []
    for school, group in terms.groupby("school"):
        enrollment = float(group["enrollment"].iloc[0])
        weight = np.zeros(len(days))
        for _, term in group.iterrows():
            weight = np.maximum(
                weight,
                _term_weight(days, term["start_date"], term["end_date"], ramp_days, timezone),
            )
        present = weight > 0
        for ts, w in zip(days[present], weight[present]):
            rows.append({"timestamp": ts, "school": school, "value": enrollment * w})

    if not rows:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(rows).reset_index(drop=True)
    print(
        f"[academic_calendar] {df['school'].nunique()} schools, {len(df)} school-days "
        f"({df['timestamp'].min().date()} -> {df['timestamp'].max().date()})."
    )
    return df


def _term_weight(
    days: pd.DatetimeIndex,
    term_start,
    term_end,
    ramp_days: int,
    timezone: str,
) -> np.ndarray:
    """0..1 in-session weight: linear ramp up at move-in, down at move-out."""
    start = pd.Timestamp(term_start).tz_localize(timezone)
    end = pd.Timestamp(term_end).tz_localize(timezone)
    ramp = pd.Timedelta(days=ramp_days)

    weight = np.zeros(len(days))
    weight[(days >= start) & (days <= end)] = 1.0

    if ramp_days > 0:
        ramp_in = (days >= start - ramp) & (days < start)
        weight[ramp_in] = np.clip((days[ramp_in] - (start - ramp)) / ramp, 0, 1)

        ramp_out = (days > end) & (days <= end + ramp)
        weight[ramp_out] = np.clip(1 - (days[ramp_out] - end) / ramp, 0, 1)

    return weight
