"""population-pulse dashboard.

Reads pre-ingested Parquet files — no API keys required.

Data source priority:
  1. Local data/boston/*.parquet  (local development after running the pipeline)
  2. GitHub data branch           (Streamlit Cloud — fetched automatically)
  3. Local sample data            (offline / first-time dev setup)

Run locally:
    streamlit run src/dashboard/app.py
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

# Streamlit Cloud runs this file directly, so the repo root is not
# automatically on sys.path. Insert it so src.* imports resolve correctly.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import requests
import streamlit as st

from src.analysis.correlate import align, lagged_cross_correlation

DATA_BRANCH_BASE = os.environ.get(
    "POPULATION_PULSE_DATA_URL",
    "https://raw.githubusercontent.com/hefrock/population-pulse/data",
)

SIGNALS = ["transit", "weather", "events", "academic_calendar", "wastewater", "hospital_demand"]

st.set_page_config(page_title="population-pulse", layout="wide")


@st.cache_data(ttl=3600)
def _load_signals(city: str) -> dict[str, pd.DataFrame]:
    """Load signals: local files → data branch → sample data."""
    data_dir = Path("data") / city

    # 1. Local Parquet files (present after running the ingestion pipeline)
    if all((data_dir / f"{s}.parquet").exists() for s in SIGNALS):
        return {s: pd.read_parquet(data_dir / f"{s}.parquet") for s in SIGNALS}

    # 2. GitHub data branch (used in Streamlit Cloud — no keys needed)
    result: dict[str, pd.DataFrame] = {}
    missing = []
    for signal in SIGNALS:
        url = f"{DATA_BRANCH_BASE}/data/{city}/{signal}.parquet"
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            result[signal] = pd.read_parquet(io.BytesIO(resp.content))
        except Exception:
            missing.append(signal)
            result[signal] = pd.DataFrame()

    if not missing:
        return result

    # 3. Local sample data (offline / CI)
    sample_dir = Path("data") / "samples"
    if sample_dir.exists():
        from src.ingestion.make_samples import main as make_samples
        make_samples()
        if all((data_dir / f"{s}.parquet").exists() for s in SIGNALS):
            return {s: pd.read_parquet(data_dir / f"{s}.parquet") for s in SIGNALS}

    return result


def _filter_by_date(
    signals: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    out = {}
    for name, df in signals.items():
        if df.empty or "timestamp" not in df.columns:
            out[name] = df
            continue
        ts = pd.to_datetime(df["timestamp"], utc=True)
        out[name] = df[ts.between(start, end)].copy()
    return out


def main() -> None:
    st.title("population-pulse")
    st.caption(
        "Do population surges — events, weather, disease — correlate with "
        "hospital ED demand? Phase 1: descriptive exploration."
    )

    with st.sidebar:
        city = st.selectbox("City", ["boston"], index=0)
        st.markdown("**Date range**")
        default_end = pd.Timestamp.now(tz="UTC").normalize()
        default_start = default_end - pd.Timedelta(days=365)
        start_date = st.date_input("From", value=default_start.date())
        end_date = st.date_input("To", value=default_end.date())
        st.markdown("---")
        st.caption(
            "Data refreshes daily via GitHub Actions. "
            "No API keys are needed to view this dashboard."
        )

    with st.spinner("Loading data…"):
        try:
            raw_signals = _load_signals(city)
        except Exception as exc:
            st.error(f"Could not load data: {exc}")
            st.info(
                "For local development run: "
                "`python -m src.ingestion.make_samples`"
            )
            return

    start_ts = pd.Timestamp(start_date, tz="UTC")
    end_ts = pd.Timestamp(end_date, tz="UTC")
    signals = _filter_by_date(raw_signals, start_ts, end_ts)

    all_empty = all(v.empty for v in signals.values())
    if all_empty:
        st.warning(
            "No data available yet. "
            "The GitHub Actions pipeline runs daily — check back after the first run, "
            "or run `python -m src.ingestion.run --city boston` locally."
        )
        return

    # --- Aligned timeline ----------------------------------------------------
    st.subheader("Signals on a shared weekly timeline")
    numeric_signals = {
        k: v for k, v in signals.items()
        if k != "events" and not v.empty
    }

    # Hospital demand has multiple metric rows per week (ILI patients +
    # total patients). Keep only the primary metric so the weekly sum in
    # align() reflects ILI patients rather than an arbitrary mixture.
    hd = numeric_signals.get("hospital_demand", pd.DataFrame())
    if not hd.empty and "metric" in hd.columns:
        primary = hd[hd["metric"] == "ili_patients"]
        numeric_signals["hospital_demand"] = primary if not primary.empty else hd

    # Wastewater is long-form (one row per pathogen). Split each pathogen into
    # its own series so they correlate independently — summing different viral
    # scales (RNA copies vs. normalized activity levels) into one number via
    # align() would be meaningless.
    ww = numeric_signals.pop("wastewater", pd.DataFrame())
    if not ww.empty and "pathogen" in ww.columns:
        for pathogen, grp in ww.groupby("pathogen"):
            numeric_signals[f"wastewater: {pathogen}"] = grp

    aligned = align(numeric_signals, resolution="W")
    if aligned.empty:
        st.warning("No numeric signals to display for this date range.")
        return

    # Standardize each signal (z-score) for the line chart.
    # Signals have incompatible units and scales (ridership in millions,
    # temperature in °C, ILI patients in hundreds) — raw values make one
    # signal dominate the chart. Correlation below uses the raw aligned frame.
    std = aligned.std()
    mean = aligned.mean()
    flat_signals = std[std == 0].index.tolist()
    if flat_signals:
        st.warning(
            f"Signal(s) {flat_signals} have zero variance in this date range "
            "and will appear as a flat line."
        )
    aligned_display = (aligned - mean) / std.replace(0, 1)
    st.line_chart(aligned_display)
    st.caption(
        "Signals shown as z-scores (mean 0, std 1) so different units "
        "are comparable. Raw values are used for the correlation below."
    )

    # --- Lagged cross-correlation -------------------------------------------
    st.subheader("Lagged cross-correlation vs. hospital demand")
    st.caption(
        "Positive lag = the driver leads hospital demand by that many weeks. "
        "Deseasonalized by default to avoid spurious winter-trend correlation."
    )

    drivers = [c for c in aligned.columns if c != "hospital_demand"]
    if "hospital_demand" not in aligned.columns or not drivers:
        st.info("Need both a driver signal and hospital_demand to correlate.")
        return

    col1, col2 = st.columns(2)
    with col1:
        driver = st.selectbox("Driver signal", drivers)
    with col2:
        deseason = st.checkbox("Deseasonalize first (recommended)", value=True)

    try:
        result = lagged_cross_correlation(
            aligned[driver], aligned["hospital_demand"],
            max_lag=8, deseasonalize=deseason,
        )
        corr_df = pd.DataFrame(
            {"lag_weeks": result.lags, "correlation": result.correlations}
        ).set_index("lag_weeks")
        st.bar_chart(corr_df)
        st.metric(
            label=f"Strongest correlation ({driver} → hospital demand)",
            value=f"{result.best_corr:+.2f}",
            delta=f"at lag {result.best_lag} weeks",
        )
        st.caption(
            "Reminder: correlation here is suggestive, not causal. "
            "Confirm with matched-baseline event studies planned for Phase 2."
        )
    except ValueError as exc:
        st.warning(str(exc))


if __name__ == "__main__":
    main()
