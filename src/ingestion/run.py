"""Ingestion entry point.

Usage:
    python -m src.ingestion.run --city boston --start 2025-01-01 --end 2025-03-31

Pulls every signal for the given city via its provider, writes each to a Parquet
file under data/<city>/, and prints a short summary. Designed to be safe to run
repeatedly (it overwrites the per-signal files) and to be the thing a scheduled
GitHub Action calls.

The ``events`` signal additionally maintains ``events_archive.parquet``: each
day's upcoming-events snapshot is folded into a running history (see
``src/ingestion/events_archive.py``) rather than overwritten, so the events
signal slowly accumulates real date overlap with historical hospital_demand.

``transit``, ``weather``, ``bikeshare``, ``academic_calendar``, ``wastewater``,
and ``hospital_demand`` all merge each fetch into their existing parquet file
in place (see ``src/ingestion/timeseries_archive.py``) instead of overwriting
it, so a wide one-time backfill plus the daily rolling fetch accumulate
permanently rather than being capped at the rolling window. This is also how
``bikeshare``'s GBFS fallback (a single "right now" snapshot) builds up a
history over time, and how ``wastewater`` absorbs upstream revisions to
already-fetched dates instead of getting stuck with a stale value forever
(the merge's "newer fetch wins" tie-break applies here on purpose).
"""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from src.ingestion import events_archive, timeseries_archive
from src.providers import load_provider

load_dotenv()

DATA_BRANCH_BASE = os.environ.get(
    "POPULATION_PULSE_DATA_URL",
    "https://raw.githubusercontent.com/hefrock/population-pulse/data",
)

# Key columns for deduplicating accumulated timeseries signals (see
# timeseries_archive.merge). transit has multiple routes per timestamp;
# weather and bikeshare are one row per timestamp; wastewater has multiple
# pathogens per timestamp; hospital_demand has multiple metrics per
# timestamp; academic_calendar has multiple schools per timestamp.
#
# Every signal not listed here is overwritten outright by each run with
# whatever window was requested -- harmless for events (handled separately
# via events_archive) but a real bug for anything whose fetcher already
# returns real historical data: the daily cron's default trailing-365-day
# window would silently erode years of accumulated history back down to one
# year on its very next run. All six signals below already return full
# historical data from their live/curated sources, so all six need to
# accumulate rather than be overwritten.
TIMESERIES_KEY_COLUMNS = {
    "transit": ["timestamp", "route"],
    "weather": ["timestamp"],
    "bikeshare": ["timestamp"],
    "academic_calendar": ["timestamp", "school"],
    "wastewater": ["timestamp", "pathogen"],
    "hospital_demand": ["timestamp", "metric"],
}


def _default_range() -> tuple[str, str]:
    """Default to the trailing 365 days."""
    end = date.today()
    start = end - timedelta(days=365)
    return start.isoformat(), end.isoformat()


def run(city: str, start: str, end: str) -> None:
    provider = load_provider(city)
    out_dir = Path("data") / city
    out_dir.mkdir(parents=True, exist_ok=True)

    signals = {
        "transit": provider.fetch_transit,
        "bikeshare": provider.fetch_bikeshare,
        "weather": provider.fetch_weather,
        "events": provider.fetch_events,
        "academic_calendar": provider.fetch_academic_calendar,
        "wastewater": provider.fetch_wastewater,
        "hospital_demand": provider.fetch_hospital_demand,
    }

    print(f"Ingesting {provider.name} from {start} to {end}")
    for name, fetch in signals.items():
        try:
            df = fetch(start, end)
            path = out_dir / f"{name}.parquet"

            if name in TIMESERIES_KEY_COLUMNS:
                archive_url = f"{DATA_BRANCH_BASE}/data/{city}/{name}.parquet"
                existing = timeseries_archive.load_existing(path, archive_url, list(df.columns))
                df = timeseries_archive.merge(existing, df, TIMESERIES_KEY_COLUMNS[name])

            df.to_parquet(path, index=False)
            print(f"  {name:16s} {len(df):6d} rows -> {path}")

            if name == "events":
                archive_path = out_dir / "events_archive.parquet"
                archive_url = f"{DATA_BRANCH_BASE}/data/{city}/events_archive.parquet"
                existing = events_archive.load_existing(archive_path, archive_url)
                archive = events_archive.merge(existing, df)
                archive.to_parquet(archive_path, index=False)
                print(f"  {'events_archive':16s} {len(archive):6d} rows -> {archive_path}")
        except Exception as exc:  # noqa: BLE001 - we want one bad source to not kill the run
            print(f"  {name:16s} FAILED: {exc}")


def main() -> None:
    default_start, default_end = _default_range()
    parser = argparse.ArgumentParser(description="Ingest population-pulse signals.")
    parser.add_argument("--city", default="boston", help="City slug (default: boston)")
    parser.add_argument("--start", default=default_start, help="ISO start date")
    parser.add_argument("--end", default=default_end, help="ISO end date")
    args = parser.parse_args()
    run(args.city, args.start, args.end)


if __name__ == "__main__":
    main()
