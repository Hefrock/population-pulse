"""Hospital-demand fetcher (the dependent variable).

Three-tier fallback:
  1. MA DPH manual CSV  — actual ED visit counts (best); download weekly from
     https://www.mass.gov/info-details/weekly-flu-report into data/ma_dph_respiratory.csv
  2. CDC FluView ILINet — automated weekly ILI proxy; no key, goes back to 1997
  3. Bundled sample     — synthetic data for offline testing only

The CDC FluView tier means the pipeline produces real (if proxy) data without
any manual steps. Swap in the MA DPH file when you want the actual ED numbers.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.ingestion import cdc_fluview
from src.ingestion.sample_window import shift_sample_to_window

CACHED_PATH = Path("data/ma_dph_respiratory.csv")
SAMPLE_PATH = Path("data/samples/hospital_demand_sample.csv")

# MA DPH revises recent weeks upward as more hospitals finish submitting their
# reports. Measured directly by diffing two refreshes of data/ma_dph_respiratory.csv
# six weeks apart (2026-07-28): revisions stay under 0.3% beyond ~8 weeks back, but
# climb sharply closer to the newest published week (0.8-0.9% at 4 weeks, 1.9-4.4%
# at 1-2 weeks, +17.5%/+20.4% at the newest week itself for ed_visits_respiratory /
# hospital_admissions_respiratory respectively). Treat any week within this many
# weeks of the latest available data as provisional, not final.
PROVISIONAL_WEEKS = 8


def provisional_cutoff(latest: pd.Timestamp) -> pd.Timestamp:
    """Weeks strictly after this cutoff are still inside MA DPH's revision window."""
    return latest - pd.Timedelta(weeks=PROVISIONAL_WEEKS)


def fetch_ma_dph_respiratory(
    metrics: list[str],
    start: str,
    end: str,
    timezone: str,
    state: str = "Massachusetts",
) -> pd.DataFrame:
    """Return weekly hospital-demand metrics as ``timestamp``, ``metric``, ``value``.

    Tries MA DPH manual cache first, then CDC FluView, then bundled sample.
    """
    # Tier 1: MA DPH manual download (actual ED data)
    if CACHED_PATH.exists():
        print("[hospital] Using MA DPH manual data.")
        return _load_csv(CACHED_PATH, metrics, start, end, timezone)

    # Tier 2: CDC FluView ILINet (automated ILI proxy)
    ili_df = cdc_fluview.fetch_ili_data(state=state, start=start, end=end, timezone=timezone)
    if not ili_df.empty:
        return ili_df

    # Tier 3: synthetic sample — offline / CI testing only
    print(
        "\n⚠️  WARNING: [hospital] Falling back to SYNTHETIC sample data.\n"
        "   Correlations computed with this data are not meaningful.\n"
        "   To use real data, either download the MA DPH file or ensure\n"
        "   CDC FluView (Delphi Epidata API) is reachable.\n"
    )
    if not SAMPLE_PATH.exists():
        raise FileNotFoundError(
            "No hospital-demand data found. Run `python -m src.ingestion.make_samples` "
            "to regenerate the sample, or download the MA DPH file."
        )
    # shift_to_window only for the synthetic sample: an aged sample must still
    # cover a today-relative window. Real MA DPH data (Tier 1) is never shifted.
    return _load_csv(SAMPLE_PATH, metrics, start, end, timezone, shift_to_window=True)


def _load_csv(
    path: Path, metrics: list[str], start: str, end: str, timezone: str,
    shift_to_window: bool = False,
) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    if shift_to_window:
        df = shift_sample_to_window(df, start, end)
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(timezone)
    mask = (
        (df["timestamp"] >= pd.Timestamp(start, tz=timezone))
        & (df["timestamp"] <= pd.Timestamp(end, tz=timezone))
        & (df["metric"].isin(metrics))
    )
    return df.loc[mask].reset_index(drop=True)
