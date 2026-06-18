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

import altair as alt
import pandas as pd
import requests
import streamlit as st

from src.analysis.correlate import align, lagged_cross_correlation, seasonal_residual
from src.analysis.regression import fit_count_regression, fit_logistic_regression

DATA_BRANCH_BASE = os.environ.get(
    "POPULATION_PULSE_DATA_URL",
    "https://raw.githubusercontent.com/hefrock/population-pulse/data",
)

SIGNALS = ["transit", "bikeshare", "weather", "events", "academic_calendar", "wastewater", "hospital_demand"]

# Shared categorical palette so a given signal gets the same color everywhere
# it appears (currently just the timeline, but keeps future charts consistent).
COLOR_SCHEME = "tableau10"

st.set_page_config(page_title="population-pulse", layout="wide")


def _local_data_fingerprint(city: str) -> tuple[float, ...]:
    """Mtime per local signal file, passed into _load_signals as a cache key.

    Without this, st.cache_data(ttl=3600) only keys on `city`, so a pipeline
    run that rewrites data/<city>/*.parquet while the dashboard process is
    still alive keeps serving the old (possibly date-range-narrower) cached
    frames for up to an hour — no widening of the sidebar date range can
    reveal data that was never reloaded into memory.
    """
    data_dir = Path("data") / city
    return tuple(
        (data_dir / f"{s}.parquet").stat().st_mtime if (data_dir / f"{s}.parquet").exists() else -1.0
        for s in SIGNALS
    )


@st.cache_data(ttl=3600)
def _load_signals(city: str, fingerprint: tuple[float, ...]) -> dict[str, pd.DataFrame]:
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


def _build_numeric_signals(signals: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Pick out the signals (and sub-series) that ``align()`` can resample.

    ``events`` is excluded here — it's shown as markers on the timeline
    instead. Hospital demand is reduced to one primary metric, and wastewater
    is split one series per pathogen, both for the reasons described inline.
    """
    numeric_signals = {
        k: v for k, v in signals.items()
        if k != "events" and not v.empty
    }

    # Hospital demand has multiple metric rows per week (e.g. ED visits +
    # admissions, or ILI patients + total patients depending on which fetcher
    # tier supplied the data). Keep only one primary metric so the weekly sum
    # in align() reflects a single series rather than an arbitrary mixture.
    # Preference order matches hospital.py's fallback tiers: MA DPH ED visits,
    # then the CDC FluView ILI proxy.
    hd = numeric_signals.get("hospital_demand", pd.DataFrame())
    if not hd.empty and "metric" in hd.columns:
        for primary_metric in ("ed_visits_respiratory", "ili_patients"):
            primary = hd[hd["metric"] == primary_metric]
            if not primary.empty:
                numeric_signals["hospital_demand"] = primary
                break
        else:
            numeric_signals["hospital_demand"] = hd

    # Wastewater is long-form (one row per pathogen). Split each pathogen into
    # its own series so they correlate independently — summing different viral
    # scales (RNA copies vs. normalized activity levels) into one number via
    # align() would be meaningless.
    ww = numeric_signals.pop("wastewater", pd.DataFrame())
    if not ww.empty and "pathogen" in ww.columns:
        for pathogen, grp in ww.groupby("pathogen"):
            numeric_signals[f"wastewater: {pathogen}"] = grp

    return numeric_signals


def _signal_freshness(raw_signals: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for name in SIGNALS:
        df = raw_signals.get(name, pd.DataFrame())
        if df.empty or "timestamp" not in df.columns:
            rows.append({"signal": name, "earliest data point": "no data", "latest data point": "no data"})
            continue
        ts = pd.to_datetime(df["timestamp"], utc=True)
        rows.append({
            "signal": name,
            "earliest data point": ts.min().date().isoformat(),
            "latest data point": ts.max().date().isoformat(),
        })
    return pd.DataFrame(rows)


def _render_overview(
    aligned: pd.DataFrame,
    raw_signals: dict[str, pd.DataFrame],
    signals: dict[str, pd.DataFrame],
) -> None:
    hd = aligned["hospital_demand"].dropna() if "hospital_demand" in aligned.columns else pd.Series(dtype=float)

    if len(hd) >= 4:
        residual = seasonal_residual(hd)
        latest_value = hd.iloc[-1]
        latest_residual = residual.iloc[-1]
        hi = residual.quantile(0.75)
        lo = residual.quantile(0.25)

        c1, c2, c3 = st.columns(3)
        c1.metric("Respiratory ED demand (latest week)", f"{latest_value:,.0f}")
        c2.metric("Vs. seasonal baseline", f"{latest_residual:+,.1f}")
        c3.metric("Weeks of data in range", f"{len(hd)}")

        if latest_residual > hi:
            st.warning(
                "**Elevated for this time of year** — respiratory ED demand in "
                "the latest week sits in the top quarter of residuals after "
                "removing the seasonal trend."
            )
        elif latest_residual < lo:
            st.info(
                "**Below baseline for this time of year** — respiratory ED "
                "demand in the latest week sits in the bottom quarter of "
                "residuals after removing the seasonal trend."
            )
        else:
            st.success(
                "**Normal for this time of year** — respiratory ED demand in "
                "the latest week is within the typical range of the seasonal "
                "trend."
            )
    else:
        st.info("Not enough respiratory ED demand data in this date range for a status summary.")

    col_events, col_freshness = st.columns(2)

    with col_events:
        st.markdown("#### Notable events in this window")
        events_df = signals.get("events", pd.DataFrame())
        if events_df.empty:
            st.caption("No events from the manual CSV, Ticketmaster, or civic calendar fall in this date range.")
        else:
            show = events_df.copy()
            show["timestamp"] = pd.to_datetime(show["timestamp"], utc=True).dt.date
            show = show.rename(columns={
                "timestamp": "date", "name": "event", "expected_attendance": "expected attendance",
            })
            cols = [c for c in ["date", "event", "venue", "expected attendance"] if c in show.columns]
            st.dataframe(
                show[cols].sort_values("date"), width="stretch", hide_index=True,
            )

    with col_freshness:
        st.markdown("#### Data freshness")
        st.dataframe(_signal_freshness(raw_signals), width="stretch", hide_index=True)
        st.caption("Earliest/latest timestamp available per signal, regardless of the date range selected above.")


def _render_timeline(aligned: pd.DataFrame, events_df: pd.DataFrame) -> None:
    st.caption(
        "Each signal is shown as a z-score (mean 0, std 1 over this date range) "
        "so series with very different units and scales — ridership, °C, "
        "respiratory ED visits — are comparable on one chart. The correlation "
        "and regression tab uses the raw values."
    )

    columns = list(aligned.columns)
    # Default to the dependent variable plus the README's most robust drivers, not all 8 signals.
    default_signals = [c for c in ("hospital_demand", "transit", "weather", "bikeshare") if c in columns] or columns
    selected = st.multiselect("Signals to show", options=columns, default=default_signals)
    if not selected:
        st.info("Select at least one signal to plot.")
        return

    std = aligned[selected].std()
    mean = aligned[selected].mean()
    flat_signals = std[std == 0].index.tolist()
    if flat_signals:
        st.warning(
            f"Signal(s) {flat_signals} have zero variance in this date range "
            "and will appear as a flat line."
        )
    z = (aligned[selected] - mean) / std.replace(0, 1)

    raw_long = aligned[selected].reset_index().melt(
        id_vars="timestamp", var_name="signal", value_name="raw_value"
    )
    z_long = z.reset_index().melt(id_vars="timestamp", var_name="signal", value_name="zscore")
    long_df = raw_long.merge(z_long, on=["timestamp", "signal"]).dropna(subset=["zscore"])

    line = alt.Chart(long_df).mark_line().encode(
        x=alt.X("timestamp:T", title="Week"),
        y=alt.Y("zscore:Q", title="Z-score (std devs from mean)"),
        color=alt.Color("signal:N", title="Signal", scale=alt.Scale(scheme=COLOR_SCHEME)),
        tooltip=[
            alt.Tooltip("timestamp:T", title="Week"),
            alt.Tooltip("signal:N", title="Signal"),
            alt.Tooltip("raw_value:Q", title="Raw value", format=",.2f"),
            alt.Tooltip("zscore:Q", title="Z-score", format="+.2f"),
        ],
    )

    chart = line
    if not events_df.empty:
        ev = events_df.copy()
        ev["timestamp"] = pd.to_datetime(ev["timestamp"], utc=True)
        rules = alt.Chart(ev).mark_rule(color="gray", strokeDash=[4, 4], opacity=0.6).encode(
            x="timestamp:T",
            tooltip=[
                alt.Tooltip("timestamp:T", title="Date"),
                alt.Tooltip("name:N", title="Event"),
                alt.Tooltip("venue:N", title="Venue"),
                alt.Tooltip("expected_attendance:Q", title="Expected attendance", format=",.0f"),
            ],
        )
        chart = line + rules
        st.caption("Dashed vertical lines mark known large events (hover for details).")

    st.altair_chart(chart.properties(height=420).interactive(), width="stretch")

    with st.expander("Aligned weekly data table"):
        st.dataframe(aligned, width="stretch")
        st.download_button(
            "Download as CSV",
            data=aligned.to_csv().encode("utf-8"),
            file_name="population_pulse_aligned_weekly.csv",
            mime="text/csv",
        )


def _render_correlation_and_regression(aligned: pd.DataFrame) -> None:
    drivers = [c for c in aligned.columns if c != "hospital_demand"]
    if "hospital_demand" not in aligned.columns or not drivers:
        st.info("Need both a driver signal and respiratory ED demand (`hospital_demand`) to correlate.")
        return

    col1, col2 = st.columns(2)
    with col1:
        driver = st.selectbox("Driver signal", drivers)
    with col2:
        deseason = st.checkbox("Deseasonalize first (recommended)", value=True)

    st.caption(
        "Positive lag = the driver leads respiratory ED demand by that many "
        "weeks. Deseasonalized by default to avoid spurious winter-trend "
        "correlation."
    )

    try:
        result = lagged_cross_correlation(
            aligned[driver], aligned["hospital_demand"],
            max_lag=8, deseasonalize=deseason,
        )
    except ValueError as exc:
        st.warning(str(exc))
        return

    corr_df = pd.DataFrame({"lag_weeks": result.lags, "correlation": result.correlations})
    bars = alt.Chart(corr_df).mark_bar().encode(
        x=alt.X("lag_weeks:O", title="Lag (weeks)"),
        y=alt.Y("correlation:Q", title="Correlation", scale=alt.Scale(domain=[-1, 1])),
        color=alt.condition(
            alt.datum.correlation > 0, alt.value("#1f77b4"), alt.value("#d62728"),
        ),
        tooltip=[
            alt.Tooltip("lag_weeks:O", title="Lag (weeks)"),
            alt.Tooltip("correlation:Q", title="Correlation", format="+.3f"),
        ],
    ).properties(height=280)
    st.altair_chart(bars, width="stretch")
    st.metric(
        label=f"Strongest correlation ({driver} → respiratory ED demand)",
        value=f"{result.best_corr:+.2f}",
        delta=f"at lag {result.best_lag} weeks",
    )

    st.markdown("#### Lagged regression: does this driver predict a surge?")
    st.caption(
        "Builds a binary 'surge' label from weeks running hot for the time of "
        "year (top quantile of the deseasonalized residual) and fits a "
        "logistic regression of that label on the driver at the chosen lag, "
        "alongside a count regression (Poisson / Negative-Binomial) of raw "
        "weekly demand on the same lagged driver."
    )

    rcol1, rcol2, rcol3 = st.columns(3)
    with rcol1:
        reg_lag = st.slider(
            "Driver lag (weeks)", min_value=0, max_value=8, value=max(result.best_lag, 0)
        )
    with rcol2:
        surge_quantile = st.slider(
            "Surge threshold (quantile)", min_value=0.5, max_value=0.95, value=0.75, step=0.05
        )
    with rcol3:
        count_family = st.selectbox(
            "Count regression family", ["negative_binomial", "poisson"], index=0
        )

    try:
        logit_result = fit_logistic_regression(
            aligned, "hospital_demand", {driver: reg_lag}, surge_quantile=surge_quantile,
        )
        m1, m2, m3 = st.columns(3)
        m1.metric("AUC-ROC (surge)", f"{logit_result.auc:.2f}")
        m2.metric("Pseudo-R² (surge)", f"{logit_result.pseudo_r_squared:.2f}")
        m3.metric(f"{driver} p-value (surge)", f"{logit_result.pvalues[driver]:.4f}")
        st.caption(
            f"{logit_result.n_surge_weeks} of {logit_result.n_obs} weeks labeled "
            f"'surge' at the {surge_quantile:.0%} quantile. AUC=0.50 is chance; "
            "this is an in-sample fit, not a validated forecast."
        )
    except ValueError as exc:
        st.info(f"Surge logistic regression: {exc}")

    try:
        count_result = fit_count_regression(
            aligned, "hospital_demand", {driver: reg_lag}, family=count_family,
        )
        c1, c2, c3 = st.columns(3)
        c1.metric(f"{driver} coefficient", f"{count_result.coefficients[driver]:+.3f}")
        c2.metric(f"{driver} p-value", f"{count_result.pvalues[driver]:.4f}")
        c3.metric("AIC", f"{count_result.aic:.1f}")
        st.caption(
            f"{count_result.family} GLM, n={count_result.n_obs}, "
            f"pseudo-R²={count_result.pseudo_r_squared:.2f}. "
            "Negative-Binomial is the better-specified model for over-dispersed "
            "weekly counts -- compare AIC across families before trusting a p-value."
        )
    except ValueError as exc:
        st.info(f"Count regression: {exc}")

    with st.expander("Methodology & caveats"):
        st.markdown(
            "- Correlation and regression here are **suggestive, not causal** — "
            "a significant lagged relationship says the driver and demand move "
            "together at that lag, not that one causes the other.\n"
            "- With roughly a year of weekly data and several lagged drivers, "
            "these models are easy to overfit — prefer fewer, better-justified "
            "lags (e.g. the one `lagged_cross_correlation` already flagged).\n"
            "- Weekly demand is heavily autocorrelated, so these are in-sample "
            "fits, not validated forecasts.\n"
            "- Confirm anything interesting with matched-baseline event studies "
            "planned for Phase 2."
        )


def main() -> None:
    st.title("population-pulse")
    st.caption(
        "Do population surges — events, weather, disease — correlate with "
        "respiratory ED demand? Phase 1: descriptive exploration."
    )
    st.caption(
        "**\"Hospital demand\" (`hospital_demand`) here means weekly "
        "respiratory-illness ED visits/admissions** (MA DPH, or CDC FluView ILI "
        "as a fallback) — not all-cause hospital demand. Predicting overall "
        "hospital demand is the long-term goal; today's data covers only the "
        "respiratory slice."
    )

    with st.sidebar:
        city = st.selectbox("City", ["boston"], index=0)

    with st.spinner("Loading data…"):
        try:
            raw_signals = _load_signals(city, _local_data_fingerprint(city))
        except Exception as exc:
            st.error(f"Could not load data: {exc}")
            st.info(
                "For local development run: "
                "`python -m src.ingestion.make_samples`"
            )
            return

    default_end = pd.Timestamp.now(tz="UTC").normalize()
    default_start = default_end - pd.Timedelta(days=365)
    earliest_per_signal = [
        pd.to_datetime(df["timestamp"], utc=True).min()
        for df in raw_signals.values()
        if not df.empty and "timestamp" in df.columns
    ]
    earliest_date = min(earliest_per_signal).date() if earliest_per_signal else default_start.date()

    def _set_date_range(start, end) -> None:
        st.session_state.date_from = start
        st.session_state.date_to = end

    with st.sidebar:
        st.markdown("**Date range**")
        st.session_state.setdefault("date_from", default_start.date())
        st.session_state.setdefault("date_to", default_end.date())

        preset_cols = st.columns(4)
        presets = [("1Y", 365), ("2Y", 730), ("5Y", 1825), ("All", None)]
        for col, (label, days_back) in zip(preset_cols, presets):
            range_start = earliest_date if days_back is None else (default_end - pd.Timedelta(days=days_back)).date()
            col.button(
                label, width="stretch",
                on_click=_set_date_range, args=(range_start, default_end.date()),
            )

        start_date = st.date_input("From", key="date_from")
        end_date = st.date_input("To", key="date_to")
        st.markdown("---")
        st.caption(
            "Data refreshes daily via GitHub Actions. "
            "No API keys are needed to view this dashboard."
        )

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

    numeric_signals = _build_numeric_signals(signals)
    aligned = align(numeric_signals, resolution="W")
    if aligned.empty:
        st.warning("No numeric signals to display for this date range.")
        return

    tab_overview, tab_timeline, tab_corr = st.tabs(
        ["Overview", "Timeline", "Correlation & Regression"]
    )

    with tab_overview:
        _render_overview(aligned, raw_signals, signals)

    with tab_timeline:
        _render_timeline(aligned, signals.get("events", pd.DataFrame()))

    with tab_corr:
        _render_correlation_and_regression(aligned)


if __name__ == "__main__":
    main()
