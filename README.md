# population-pulse

**Does a city's population activity — events, weather, disease — predict pressure on hospital emergency departments?**

This project builds a data pipeline and dashboard to explore that question for Boston, with the architecture ready for other cities. It ingests real signals daily, aligns them on a common timeline, and lets you run lagged correlation analysis between any driver signal and hospital demand.

---

## Live dashboard

> Deploy to [Streamlit Community Cloud](https://share.streamlit.io) — connect this repo, set main file to `src/dashboard/app.py`, and deploy. No API keys needed in the dashboard.

---

## What it does

1. **Ingests four signals daily** via GitHub Actions:
   - Transit volume (MBTA gated station entries — historical daily ridership)
   - Weather (temperature, apparent temperature, precipitation)
   - Events (Sports and Music events via Ticketmaster; civic events via Boston.gov)
   - Hospital demand (CDC FluView ILI patient counts; upgradeable to MA DPH ED data)

2. **Stores data on a `data` branch** — the dashboard reads from there, so the app has no secrets or API calls of its own.

3. **Visualizes signals on a shared weekly timeline** and runs lagged cross-correlation to find how many weeks a driver signal leads hospital demand.

---

## The hypothesis

Four sub-hypotheses, each with a different expected lag:

| Driver | Mechanism | Expected lag |
|--------|-----------|-------------|
| Large gatherings | Acute injuries, alcohol, cardiac events | Hours to same-day |
| Weather | Heat stress, cold, asthma, falls | Same-day to a few days |
| Disease surges | Infection incubation → illness | Days to ~2 weeks |
| Daily commute | Accidents, baseline exposure | Same-day |

These are analyzed separately because they'd confound each other in a single correlation — everything trends together in winter.

---

## Data sources

| Signal | Source | Cadence | Key required |
|--------|--------|---------|-------------|
| Transit | MBTA Gated Station Entries (MassDOT open data) | Daily, 2014–present | None |
| Transit (fallback) | MBTA V3 API | Real-time snapshot | Free — `api-v3.mbta.com` |
| Weather | Open-Meteo archive | Hourly historical | None |
| Events | Ticketmaster Discovery API | Upcoming, rolling 365 days | Free — `developer.ticketmaster.com` |
| Events (civic) | Boston.gov public calendar (Drupal JSON:API) | Upcoming, rolling 365 days | None |
| Hospital demand | CDC FluView via Delphi Epidata | Weekly ILI counts | None |
| Hospital demand (better) | MA DPH Respiratory Dashboard | Weekly ED visits | Manual download |

**On hospital data:** CDC FluView provides real ILI (influenza-like illness) patient counts from sentinel providers — a good proxy, fully automated. For actual ED visit counts, download the weekly Excel file from [mass.gov/info-details/weekly-flu-report](https://www.mass.gov/info-details/weekly-flu-report) and save it as `data/ma_dph_respiratory.csv`. The pipeline will use it automatically.

**Note:** Real-time, hospital-specific bed counts are not publicly available. Weekly syndromic data sets the natural time resolution of the analysis.

---

## Quickstart (local development)

```bash
# Install dependencies
pip install -r requirements.txt

# Generate synthetic sample data (lets you run the dashboard immediately)
python -m src.ingestion.make_samples

# Launch the dashboard
streamlit run src/dashboard/app.py
```

To ingest **real data**, copy `.env.example` to `.env`, add your API keys, then run:

```bash
python -m src.ingestion.run --city boston
```

---

## Automated pipeline

GitHub Actions runs the full ingestion daily at 2 AM ET. After each run, the fresh Parquet files are pushed to the `data` branch. The dashboard reads from there — no server-side secrets needed.

To trigger a manual run: **Actions → Daily Ingestion → Run workflow**.

API keys (MBTA, Ticketmaster) are stored as GitHub Secrets and never touch the codebase.

---

## Architecture

City-specific logic is isolated behind a `CityDataProvider` interface. Adding a second city means writing one new provider and one new YAML config — nothing else changes.

```
population-pulse/
├── cities/              # per-city config (boston.yaml)
├── src/
│   ├── providers/       # abstract base + city implementations
│   ├── ingestion/       # source fetchers (mbta, weather, ticketmaster, ...)
│   ├── analysis/        # timeline alignment + lagged correlation
│   └── dashboard/       # Streamlit app
├── tests/               # pytest suite (24 tests)
├── docs/                # narrated walkthrough
└── .github/workflows/   # daily ingestion + test gate
```

**Tech stack:** Python, pandas, scipy/statsmodels, Streamlit, Parquet, GitHub Actions.

---

## Interpreting results

Correlation here is suggestive, not causal. Transit ridership, ED visits, and disease all rise and fall with the seasons, so the analysis deseasonalizes by default before computing lags. Treat early results as leads to investigate, not conclusions.

Phase 2 (planned) will run matched-baseline event studies — comparing event days against similar non-event days — to tighten the estimates.

---

## Project status

| Component | Status |
|-----------|--------|
| Ingestion pipeline | Working — MBTA, weather, Ticketmaster, CDC FluView |
| Daily GitHub Actions | Working |
| Dashboard | Working — reads from data branch, no secrets needed |
| Test suite | 24 tests, all passing |
| Phase 2 event studies | Planned |
| Second city | Architecture ready |

---

## License

MIT — see `LICENSE`.
