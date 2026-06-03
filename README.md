# population-pulse

**Do measurable surges in a city's population — driven by events, weather, and
disease — correlate with increased demand on hospital emergency departments?**

`population-pulse` is a data pipeline and analysis toolkit for testing that
hypothesis. It started with Boston and is built to be refactored for other
cities later.

---

## The hypothesis

The core claim has four sub-hypotheses, one per type of population-driving
"event". Each has a different underlying mechanism and a different expected lag
between the population signal and any hospital-demand response:

| Event type        | Mechanism                                   | Expected lag      |
|-------------------|---------------------------------------------|-------------------|
| Large gatherings  | Acute injuries, alcohol, cardiac events     | Hours to same-day |
| Weather/seasonal  | Heat stress, cold, asthma, falls            | Same-day to days  |
| Disease surges    | Infection incubation → illness              | Days to ~2 weeks  |
| Daily commute     | Baseline accidents, exposure                | Same-day          |

These are tested separately because a single correlation analysis across all of
them would be confounded by shared seasonality (everything trends together in
winter).

## What this is (and isn't)

This is a staged project. Each phase is independently useful:

- **Phase 1 — Descriptive correlation engine** *(current)*: build the ingestion
  pipeline, align signals on a common timeline, and visualize them together.
- **Phase 2 — Event-impact quantification**: estimate effect sizes and lags for
  discrete events (a playoff run, a heat wave, a flu surge).
- **Phase 3 — Nowcasting tool**: predict near-term ED demand pressure from
  today's flow, event, and weather signals.

It is **not** a real-time hospital bed tracker. See "Data reality" below.

## Data reality (read this before you get excited)

Real-time, hospital-specific bed counts are **not** publicly available to
private individuals. The realistic hospital-demand signal is **weekly
emergency-department syndromic data** published by Massachusetts DPH and the
Boston Public Health Commission. That sets the natural time resolution of the
*analysis* to daily-to-weekly, even though some input signals (transit) are
minute-level and get aggregated up.

### Data sources

| Signal              | Source                                      | Cadence     | Access        |
|---------------------|---------------------------------------------|-------------|---------------|
| Transit ridership   | MBTA V3 API (`api-v3.mbta.com`)             | Real-time   | Free, API key |
| Traffic / incidents | MassDOT, Analyze Boston open data           | Varies      | Free          |
| Weather             | NWS / Open-Meteo                            | Hourly      | Free          |
| Large events        | Event calendars (venue schedules)           | As-scheduled| Free/scrape   |
| **Hospital demand** | MA DPH Respiratory Illness Dashboard / BPHC | **Weekly**  | Free (public) |
| Hospital locations  | MassGIS Acute Care Hospitals                | Static      | Free          |

The gold-standard source — CDC's NSSP/ESSENCE real-time ED feed — is restricted
to public-health jurisdictions. If you gain a public-health affiliation, that
becomes accessible and unlocks daily resolution.

## Architecture

City-specific logic is isolated behind a common `CityDataProvider` interface, so
adding a second city later means writing a new provider, not rewiring the
pipeline. Boston is simply the first implementation.

```
population-pulse/
├── cities/              # per-city configuration (boston.yaml)
├── src/
│   ├── providers/       # abstract base + city implementations
│   ├── ingestion/       # source-specific fetchers (MBTA, weather, ...)
│   ├── analysis/        # timeline alignment + correlation logic
│   └── dashboard/       # Streamlit app
├── docs/                # narrated, phase-by-phase tutorial
├── notebooks/           # exploratory analysis
└── .github/workflows/   # scheduled ingestion
```

## Tech stack

- **Python** for ingestion, alignment, and statistics (`pandas`, `statsmodels`,
  `scipy`)
- **Streamlit** for the dashboard (pure-Python; swappable for a JS frontend in
  Phase 3)
- **Flat files** (Parquet/CSV) for storage to start; migrate to DuckDB/SQLite
  when needed
- **GitHub Actions** for scheduled data pulls

## Quickstart

```bash
pip install -r requirements.txt

# copy the example env and add your MBTA API key (free)
cp .env.example .env

# fetch a sample of data for Boston
python -m src.ingestion.run --city boston

# launch the dashboard
streamlit run src/dashboard/app.py
```

See [`docs/01-getting-started.md`](docs/01-getting-started.md) for the full
narrated walkthrough.

## Status

Phase 1 scaffolding. The MBTA fetcher and the alignment/correlation skeleton are
in place; other fetchers are stubbed with clear TODOs. The dashboard runs on
sample data so you can see the shape of things before wiring live keys.

## A note on interpretation

Correlation here is suggestive, not causal. Transit ridership, ED visits, and
disease all rise and fall with the calendar, so the analysis deliberately
separates event types, uses lagged cross-correlation, and (in Phase 2) compares
event days against matched non-event baseline days. Treat early results as
hypotheses to investigate, not conclusions.

## License

MIT (see `LICENSE`).
