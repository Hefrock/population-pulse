# population-pulse

**Does a city's population activity — events, weather, disease — predict pressure on hospital emergency departments?**

This project builds a data pipeline and dashboard to explore that question for Boston, with the architecture ready for other cities. It ingests real signals daily, aligns them on a common timeline, and lets you run lagged correlation analysis between any driver signal and hospital demand.

---

## Live dashboard

> Deploy to [Streamlit Community Cloud](https://share.streamlit.io) — connect this repo, set main file to `src/dashboard/app.py`, and deploy. No API keys needed in the dashboard.

---

## What it does

1. **Ingests six signals daily** via GitHub Actions:
   - Transit volume (MBTA gated station entries — historical daily ridership, 2014–present)
   - Weather (temperature, apparent temperature, precipitation)
   - Events (Sports and Music events via Ticketmaster; civic events via Boston.gov)
   - Academic calendar (student population in/out of the city — ~150K students across 8 universities)
   - Wastewater viral surveillance (SARS-CoV-2, Influenza A/B, RSV — real Deer Island data via WastewaterSCAN, the leading indicator of respiratory demand)
   - Hospital demand (MA DPH weekly ED visits + admissions, 2019–present — the dependent variable; CDC FluView ILI is the automated fallback)

2. **Stores data on a `data` branch** — the dashboard reads from there, so the app has no secrets or API calls of its own.

3. **Visualizes signals on a shared weekly timeline** and runs lagged cross-correlation to find how many weeks a driver signal leads hospital demand.

---

## The hypothesis

Four sub-hypotheses, each with a different expected lag:

| Driver | Mechanism | Signals | Expected lag |
|--------|-----------|---------|-------------|
| Large gatherings | Acute injuries, alcohol, cardiac events | Events, academic calendar | Hours to same-day |
| Weather | Heat stress, cold, asthma, falls | Weather | Same-day to a few days |
| Disease surges | Infection incubation → illness | Wastewater (leading), academic calendar | Days to ~2 weeks |
| Daily commute | Accidents, baseline exposure | Transit | Same-day |

These are analyzed separately because they'd confound each other in a single correlation — everything trends together in winter.

**Wastewater is the standout leading indicator:** respiratory virus shed in stool appears in municipal sewage ~4–10 days *before* the matching clinical surge, so it's expected to *precede* hospital demand (a positive lead lag is the result we look for). The **academic calendar** is a large population driver — the ~150K students who arrive and leave each semester move the city's denominator on the scale of a major event.

---

## Data sources

| Signal | Source | Cadence | Key required |
|--------|--------|---------|-------------|
| Transit | MBTA Gated Station Entries (MassDOT open data) | Daily, 2014–present | None |
| Transit (fallback) | MBTA V3 API | Real-time snapshot | Free — `api-v3.mbta.com` |
| Weather | Open-Meteo archive | Hourly historical | None |
| Events | Ticketmaster Discovery API | Upcoming, rolling 365 days | Free — `developer.ticketmaster.com` |
| Events (civic) | Boston.gov public calendar (Drupal JSON:API) | Upcoming, rolling 365 days | None |
| Academic calendar | Curated term dates (8 universities), validated against registrars | Per semester | None — curated CSV |
| Wastewater (SARS-CoV-2, Flu A/B, RSV) | WastewaterSCAN (Stanford/Emory), Deer Island plant | Twice-weekly, 2022–present | None (undocumented public endpoint) |
| Wastewater (fallback, SARS-CoV-2) | MWRA Deer Island / Biobot (metro-Boston) | ~Daily | None — needs `data_url` set, see below |
| Wastewater (fallback, multi-pathogen) | CDC NWSS Wastewater Viral Activity Level (Socrata) | Weekly | None |
| Hospital demand | MA DPH Respiratory Dashboard ("Visits by week") | Weekly ED visits + admissions, 2019–present | Manual download |
| Hospital demand (fallback) | CDC FluView via Delphi Epidata | Weekly ILI counts | None |

**On hospital data:** `data/ma_dph_respiratory.csv` (statewide weekly ED visits and admissions for "broad acute respiratory" diagnoses, 2019–present, 722 rows) is checked in and is the pipeline's Tier 1 — CDC FluView's ILI proxy is now only a fallback. To refresh it, download the current "Respiratory Disease Reporting" workbook from [mass.gov/info-details/weekly-flu-report](https://www.mass.gov/info-details/weekly-flu-report) (its "Visits by week" sheet covers all prior seasons) and run:

```bash
python scripts/build_ma_dph_csv.py path/to/RespiratoryDiseaseReporting*.xlsx
```

**On the academic calendar:** there is no API for university term dates, so `data/boston_academic_calendar.csv` is a hand-curated reference (the same "manual baseline" pattern as the events CSV). Term dates are validated against each school's registrar and carry a `source` column (`verified-*`, `estimate`, or `prior-year-pattern`); refresh it ~10 minutes once a year when schools publish their next calendar.

**On wastewater:** [WastewaterSCAN](https://www.wastewaterscan.org/) publishes real twice-weekly viral concentrations for the Deer Island (metro-Boston) plant back to December 2022, covering all four target pathogens — this is now Tier 1 and is the source behind the correlation results below. It's reached through an undocumented-but-public JSON endpoint (no key, but no SLA either), so MWRA and CDC NWSS remain as fallbacks if it goes away. MWRA's tier is currently inert in practice — its machine-readable export URL moves over time and `wastewater.mwra.data_url` has never been set in `cities/boston.yaml`, so that fallback has not actually been exercised against live data.

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
│   ├── ingestion/       # source fetchers (mbta, weather, ticketmaster,
│   │                    #   academic_calendar, wastewater, ...)
│   ├── analysis/        # timeline alignment, lagged correlation, count/surge regression
│   └── dashboard/       # Streamlit app
├── tests/               # pytest suite (54 tests)
├── docs/                # narrated walkthrough
└── .github/workflows/   # daily ingestion + test gate
```

**Tech stack:** Python, pandas, scipy/statsmodels, Streamlit, Parquet, GitHub Actions.

---

## Interpreting results

Correlation here is suggestive, not causal. Transit ridership, ED visits, and disease all rise and fall with the seasons, so the analysis deseasonalizes by default before computing lags. Treat early results as leads to investigate, not conclusions.

Phase 2 (planned) will run matched-baseline event studies — comparing event days against similar non-event days — to tighten the estimates.

---

## What we've found so far

This is the honest result of running the pipeline end-to-end on ~3.5 years of
real data (WastewaterSCAN wastewater + MA DPH ED visits, Dec 2022 – May 2026,
~180 weekly observations). `src/analysis/regression.py` turns each week into a
binary "surge" label (deseasonalized ED-visit residual in the top quartile —
i.e. running hot *for that time of year*) and fits a logistic regression of
each wastewater pathogen against it:

| Pathogen | Best lag | AUC-ROC | p-value |
|---|---|---|---|
| Influenza A | 0 weeks | 0.68 | 0.0007 |
| RSV | 0 weeks | 0.67 | 0.0002 (significant out to +3 weeks) |
| SARS-CoV-2 | — | ~0.5–0.6 | not significant at any lag 0–8 |

**Reads as a real, modest signal:** same-week Influenza A and RSV wastewater
levels meaningfully separate "surge" from "normal" weeks (0.5 = chance), each
individually significant. RSV's signal persists for a few weeks, which is at
least directionally consistent with the "wastewater leads clinical demand"
hypothesis, though "lag 0" at weekly resolution doesn't prove a multi-day
lead. SARS-CoV-2 wastewater shows nothing useful for surge prediction in this
window — a negative result, not a bug.

**A methodology lesson that's now baked into the code:** the same drivers fit
with a Poisson GLM (`fit_count_regression(..., family="poisson")`) come out
*highly* "significant" (p≈0) with an AIC an order of magnitude worse than the
Negative-Binomial fit, because weekly ED-visit counts are heavily
overdispersed and Poisson assumes variance = mean. The Negative-Binomial fit
on the same data shows neither pathogen significant once the AIC is corrected
for overdispersion. Lesson: **don't trust a Poisson p-value on real syndromic
count data** — always compare against `family="negative_binomial"`.

---

## Known limitations

In the spirit of an honest status report, not just a feature list:

- **Weather has no fallback tier.** Every other signal degrades to a synthetic
  sample if its real source is unreachable (the project's stated convention);
  `weather.py` doesn't — if Open-Meteo is down or blocked, `run.py` catches
  the exception and `weather.parquet` is simply not written for that run.
- **The MWRA wastewater fallback has never run against live data.** Its
  machine-readable export URL moves over time and `wastewater.mwra.data_url`
  has never been set, so that tier is unexercised code. WastewaterSCAN (Tier
  1) covers all four pathogens, so this is low-priority — but it's not the
  "tested fallback" the docstrings imply.
- **No CI runs on pull requests.** The only workflow (`ingest.yml`) triggers
  on `schedule` / `workflow_dispatch`; its `pytest tests/ -v` job gates the
  daily ingestion run on `main`, but a PR branch gets no automated test
  feedback before merge.
- **`src/ingestion/eventbrite.py` is dead code.** It was superseded by
  `civic_events.py` (Boston.gov) but the file, its `.env.example` entry, and
  its GitHub Actions secret are still present and unused by `BostonProvider`.
- **The regression module isn't wired into the dashboard yet.** `correlate.py`
  (cross-correlation) drives the Streamlit UI; `regression.py` (count
  regression + the surge logistic regression above) is currently
  analysis-only, run via ad-hoc scripts against the Parquet data.
- **Second city is unbuilt.** The `CityDataProvider` abstraction is designed
  to make a second city "one YAML + one provider class," but that claim has
  never actually been tested against a real second city.
- **Live-API fetchers are tested against mocked payloads, not real endpoints,
  in development.** The sandbox's network allowlist returns 403 for most data
  hosts (Ticketmaster, MBTA ArcGIS, mass.gov), so `pytest` covers these with
  captured payloads and the daily GitHub Actions run (full network access) is
  the actual integration test — confirmed working: the `data` branch has 1,950
  rows of real MBTA gated-entry ridership and 596 real events.

---

## Project status

| Component | Status |
|-----------|--------|
| Ingestion pipeline | Working — transit, weather, events, academic calendar, wastewater, hospital demand |
| Daily GitHub Actions | Working (no PR-level CI — see Known limitations) |
| Dashboard | Working — reads from data branch, no secrets needed; per-pathogen wastewater series |
| Cross-correlation analysis | Working — `src/analysis/correlate.py`, used by the dashboard |
| Count + surge regression | Working, analysis-only — `src/analysis/regression.py` (Poisson/NB count models, surge-label logistic regression with AUC-ROC) |
| Test suite | 54 tests, all passing |
| Phase 2 event studies | Planned |
| Second city | Architecture ready, untested |

---

## License

MIT — see `LICENSE`.
