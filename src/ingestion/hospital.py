"""Hospital-demand fetcher (the dependent variable).

This is the hardest signal to automate and the most important to get right, so
read this carefully.

Massachusetts DPH publishes weekly ED-visit and hospital-admission data for
respiratory illness, and 100% of MA emergency departments report into the
NSSP/ESSENCE platform behind it. But the *public* surface is a Tableau-style
dashboard plus a downloadable data file that is republished weekly — there is no
clean public JSON API. The real-time ESSENCE feed exists but is restricted to
public-health jurisdictions.

So Phase 1 handles this in two layers:

1. ``fetch_ma_dph_respiratory`` — the interface method. It reads a locally
   cached copy of the weekly data file (``data/ma_dph_respiratory.csv``) that
   you download from the dashboard. If it's absent, it falls back to a bundled
   sample so the pipeline runs end-to-end.

2. A documented manual refresh step (see ``REFRESH.md`` note below) until/unless
   we automate the dashboard download in Phase 2.

This honest design keeps the dependent variable trustworthy rather than scraping
something fragile and pretending it's real-time.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

CACHED_PATH = Path("data/ma_dph_respiratory.csv")
SAMPLE_PATH = Path("data/samples/hospital_demand_sample.csv")


def fetch_ma_dph_respiratory(
    metrics: list[str],
    start: str,
    end: str,
    timezone: str,
) -> pd.DataFrame:
    """Return weekly hospital-demand metrics as ``timestamp``, ``metric``,
    ``value``.

    Prefers the manually-refreshed cache; falls back to the bundled sample.

    Phase 2 TODO: automate the weekly dashboard download. The dashboard lives at
    https://www.mass.gov/info-details/weekly-flu-report and links a downloadable
    data file that is replaced each Thursday. Environmental hospitalizations
    (asthma, heat stress, COPD) for the weather sub-hypothesis come from
    https://www.mass.gov/info-details/environmental-hospitalization-data
    """
    source = CACHED_PATH if CACHED_PATH.exists() else SAMPLE_PATH
    if not source.exists():
        raise FileNotFoundError(
            "No hospital-demand data found. Download the weekly file from the "
            "MA DPH respiratory dashboard into data/ma_dph_respiratory.csv, or "
            "run `python -m src.ingestion.make_samples` for a placeholder."
        )
    if source is SAMPLE_PATH:
        print("[hospital] Using SAMPLE hospital-demand data (not real DPH data).")

    df = pd.read_csv(source, parse_dates=["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(timezone)

    mask = (
        (df["timestamp"] >= pd.Timestamp(start, tz=timezone))
        & (df["timestamp"] <= pd.Timestamp(end, tz=timezone))
        & (df["metric"].isin(metrics))
    )
    return df.loc[mask].reset_index(drop=True)
