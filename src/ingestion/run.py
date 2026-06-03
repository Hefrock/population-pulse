"""Ingestion entry point.

Usage:
    python -m src.ingestion.run --city boston --start 2025-01-01 --end 2025-03-31

Pulls every signal for the given city via its provider, writes each to a Parquet
file under data/<city>/, and prints a short summary. Designed to be safe to run
repeatedly (it overwrites the per-signal files) and to be the thing a scheduled
GitHub Action calls.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

from src.providers import load_provider


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
        "weather": provider.fetch_weather,
        "events": provider.fetch_events,
        "hospital_demand": provider.fetch_hospital_demand,
    }

    print(f"Ingesting {provider.name} from {start} to {end}")
    for name, fetch in signals.items():
        try:
            df = fetch(start, end)
            path = out_dir / f"{name}.parquet"
            df.to_parquet(path, index=False)
            print(f"  {name:16s} {len(df):6d} rows -> {path}")
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
