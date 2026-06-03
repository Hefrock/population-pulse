"""population-pulse dashboard.

A pure-Python Streamlit app that ties the pipeline together visually:

  - pick a city and date range
  - see each signal on a shared timeline
  - run a lagged cross-correlation between a chosen driver and hospital demand

Run with:
    streamlit run src/dashboard/app.py

In Phase 1 this runs on sample data out of the box. Once you've ingested real
data (python -m src.ingestion.run), it'll pick up the Parquet files under
data/<city>/.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from src.analysis.correlate import align, lagged_cross_correlation
from src.providers import load_provider

st.set_page_config(page_title="population-pulse", layout="wide")


@st.cache_data
def _load_signals(city: str, start: str, end: str) -> dict[str, pd.DataFrame]:
    """Load from ingested Parquet if present, else fetch via the provider."""
    data_dir = Path("data") / city
    names = ["transit", "weather", "events", "hospital_demand"]
    if all((data_dir / f"{n}.parquet").exists() for n in names):
        return {n: pd.read_parquet(data_dir / f"{n}.parquet") for n in names}

    provider = load_provider(city)
    return {
        "transit": provider.fetch_transit(start, end),
        "weather": provider.fetch_weather(start, end),
        "events": provider.fetch_events(start, end),
        "hospital_demand": provider.fetch_hospital_demand(start, end),
    }


def main() -> None:
    st.title("population-pulse")
    st.caption(
        "Do population surges — events, weather, disease — correlate with "
        "hospital ED demand? Phase 1: descriptive exploration."
    )

    with st.sidebar:
        city = st.selectbox("City", ["boston"], index=0)
        start = st.text_input("Start date", "2024-06-01")
        end = st.text_input("End date", "2025-05-31")
        st.markdown("---")
        st.markdown(
            "Running on **sample data** unless you've ingested real data. "
            "See `docs/01-getting-started.md`."
        )

    try:
        signals = _load_signals(city, start, end)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not load data: {exc}")
        st.info("Try: `python -m src.ingestion.make_samples` then reload.")
        return

    # --- Aligned timeline ----------------------------------------------------
    st.subheader("Signals on a shared weekly timeline")
    numeric_signals = {
        k: v for k, v in signals.items()
        if k != "events" and not v.empty
    }
    aligned = align(numeric_signals, resolution="W")
    if aligned.empty:
        st.warning("No numeric signals to display yet.")
        return
    st.line_chart(aligned)

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
            "Reminder: correlation here is suggestive, not causal. Confirm with "
            "the matched-baseline event studies planned for Phase 2."
        )
    except ValueError as exc:
        st.warning(str(exc))


if __name__ == "__main__":
    main()
