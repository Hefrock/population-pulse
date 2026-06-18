# population-pulse

[![Tests](https://github.com/hefrock/population-pulse/actions/workflows/test.yml/badge.svg)](https://github.com/hefrock/population-pulse/actions/workflows/test.yml)
[![Daily Ingestion](https://github.com/hefrock/population-pulse/actions/workflows/ingest.yml/badge.svg)](https://github.com/hefrock/population-pulse/actions/workflows/ingest.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](requirements.txt)

**Does a city's population activity — events, weather, disease — predict pressure on hospital emergency departments?**

This project builds a data pipeline and dashboard to explore that question for Boston, with the architecture ready for other cities. It ingests real signals daily, aligns them on a common timeline, and lets you run lagged correlation analysis between any driver signal and hospital demand.

**Current scope:** the only real `hospital_demand` data available today is
**respiratory-illness** ED visits/admissions (MA DPH, with CDC FluView ILI as a
fallback) — not all-cause hospital demand. Predicting *overall* hospital demand is
the long-term goal of this project; until an all-cause source is added, every
"hospital demand" result here (charts, dashboard, correlations below) is a
respiratory-demand proxy. See "Known limitations" for details.

---

## Contents

- [Live dashboard](#live-dashboard)
- [What it does](#what-it-does)
- [The hypothesis](#the-hypothesis)
- [Data sources](#data-sources)
- [Quickstart (local development)](#quickstart-local-development)
- [Automated pipeline](#automated-pipeline)
- [Architecture](#architecture)
- [Interpreting results](#interpreting-results)
- [What we've found so far](#what-weve-found-so-far)
- [Known limitations](#known-limitations)
- [Project status](#project-status)

---

## Live dashboard

> Deploy to [Streamlit Community Cloud](https://share.streamlit.io) — connect this repo, set main file to `src/dashboard/app.py`, and deploy. No API keys needed in the dashboard.

---

## What it does

1. **Ingests seven signals daily** via GitHub Actions:
   - Transit volume (MBTA gated station entries — historical daily ridership, 2014–present)
   - Bikeshare volume (Bluebikes trip-history archive — real daily ride counts, 2018–present, with a GBFS station-status fallback)
   - Weather (temperature, apparent temperature, precipitation)
   - Events (Sports and Music events via Ticketmaster; civic events via Boston.gov)
   - Academic calendar (student population in/out of the city — ~150K students across 8 universities)
   - Wastewater viral surveillance (SARS-CoV-2, Influenza A/B, RSV — real Deer Island data via WastewaterSCAN, the leading indicator of respiratory demand)
   - Hospital demand — **respiratory-illness** ED visits + admissions (MA DPH weekly data, 2019–present — the dependent variable; CDC FluView ILI is the automated fallback). Not all-cause hospital demand; see "Known limitations"

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
| Daily commute | Accidents, baseline exposure | Transit, bikeshare | Same-day |

These are analyzed separately because they'd confound each other in a single correlation — everything trends together in winter.

**Wastewater is the standout leading indicator:** respiratory virus shed in stool appears in municipal sewage ~4–10 days *before* the matching clinical surge, so it's expected to *precede* hospital demand (a positive lead lag is the result we look for). The **academic calendar** is a large population driver — the ~150K students who arrive and leave each semester move the city's denominator on the scale of a major event.

---

## Data sources

| Signal | Source | Cadence | Key required |
|--------|--------|---------|-------------|
| Transit | MBTA Gated Station Entries (MassDOT open data) | Daily, 2014–present | None |
| Transit (fallback) | MBTA V3 API | Real-time snapshot | Free — `api-v3.mbta.com` |
| Bikeshare | Bluebikes trip-history archive (S3) | Daily ride counts, 2018–present (~1-2 month publication lag) | None |
| Bikeshare (fallback) | Bluebikes GBFS `station_status` | Real-time snapshot (bikes docked, accumulated daily) | None |
| Weather | Open-Meteo archive | Hourly historical | None |
| Events | Ticketmaster Discovery API | Upcoming, rolling 365 days | Free — `developer.ticketmaster.com` |
| Events (civic) | Boston.gov public calendar (Drupal JSON:API) | Upcoming, rolling 365 days | None |
| Academic calendar | Curated term dates (8 universities), validated against registrars | Per semester | None — curated CSV |
| Wastewater (SARS-CoV-2, Flu A/B, RSV) | WastewaterSCAN (Stanford/Emory), Deer Island plant | Twice-weekly, 2022–present | None (undocumented public endpoint) |
| Wastewater (fallback, SARS-CoV-2) | MWRA Deer Island / Biobot (metro-Boston) | ~Daily | None — needs `data_url` set, see below |
| Wastewater (fallback, multi-pathogen) | CDC NWSS Wastewater Viral Activity Level (Socrata) | Weekly | None |
| Hospital demand (respiratory-illness only) | MA DPH Respiratory Dashboard ("Visits by week") | Weekly ED visits + admissions, 2019–present | Manual download |
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
├── tests/               # pytest suite (67 tests)
├── docs/                # narrated walkthrough
└── .github/workflows/   # daily ingestion + PR test gate
```

**Tech stack:** Python, pandas, scipy/statsmodels, Streamlit, Parquet, GitHub Actions.

---

## Interpreting results

Correlation here is suggestive, not causal. Transit ridership, ED visits, and disease all rise and fall with the seasons, so the analysis deseasonalizes by default before computing lags. Treat early results as leads to investigate, not conclusions.

Phase 2 (planned) will run matched-baseline event studies — comparing event days against similar non-event days — to tighten the estimates.

---

## What we've found so far

**tl;dr:** Wastewater Influenza A and RSV levels (same week, lag 0) are real,
modest predictors of a respiratory ED "surge" week (AUC-ROC 0.67–0.68, both
p<0.01) — the project's strongest result so far, though see the note below on
re-running this since the transit/weather backfill (the wastewater lag-0
finding did not reproduce cleanly — flagged, not yet resolved). Now that
transit, weather, and the new bikeshare signal have ~7 years of real history
(up from 47–81 weeks), transit and weather **both reach significance** in the
Negative-Binomial count model (p=0.011, p=0.024) — reversing the earlier
"none of the three survive correction" conclusion — and bikeshare's surge
logistic regression (AUC=0.66) is the strongest of any driver tested so far.
Academic calendar is unchanged (its real history wasn't part of this
backfill). Details and caveats below.

This is the honest result of running the pipeline end-to-end on ~3.5 years of
real data (WastewaterSCAN wastewater + MA DPH respiratory ED visits, Dec 2022 –
May 2026, ~180 weekly observations). `src/analysis/regression.py` turns each
week into a binary "surge" label (deseasonalized respiratory ED-visit residual
in the top quartile — i.e. running hot *for that time of year*) and fits a
logistic regression of each wastewater pathogen against it:

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

### The other three sub-hypotheses, on real data

A one-time wide `workflow_dispatch` backfill (2018-07-01 → present) has now
run for transit, weather, and the new bikeshare signal — see "Known
limitations" for how — so these three go from 47–81 weeks of real history to
~7 years, finally longer than wastewater's ~3.5. Each is tested the same way:
best lag picked by `lagged_cross_correlation` (max lag 8, confirmed against a
wider 16-week search — none of the "best" lags below are search-boundary
artifacts), then fit with `fit_logistic_regression` (surge AUC-ROC) and
`fit_count_regression` (Poisson vs. Negative-Binomial):

| Driver | Real-data window | Weeks | Best lag | Surge AUC-ROC (p) | Poisson AIC | NB AIC | NB p-value |
|---|---|---|---|---|---|---|---|
| Academic calendar (large gatherings) | Aug 2024 – May 2026 | 81 | 0 | 0.64 (p=0.048) | 60,771 | 1,632 | 0.65 |
| Bikeshare (active/leisure mobility) | Jun 2019 – May 2026 | 358 | 0 | 0.66 (p<0.0001) | 348,257 | 7,130 | 0.07 |
| Transit (commute) | Jun 2019 – May 2026 | 358 | 8 | 0.57 (p=0.021) | 346,385 | 7,187 | 0.011 |
| Weather (temperature) | Jun 2019 – May 2026 | 361 | 5 | 0.62 (p=0.001) | 340,487 | 7,187 | 0.024 |

**The backfill changed the answer, not just the sample size:** with ~7 years
of real data instead of <1, transit and weather **both reach significance**
in the Negative-Binomial count model (p=0.011 and p=0.024) — reversing the
earlier "none of the three survive correction" conclusion from when this
table only had 47–48 weeks per driver. Bikeshare's surge-logistic AUC (0.66)
is the strongest of any driver tested so far, including wastewater, though
its NB p-value (0.07) falls just short of conventional significance.
Academic calendar is unchanged from before (its real history wasn't part of
this backfill) and remains the same single borderline result it was.

**Caveat on transit's lag:** lag 8 (two months) is a long delay for a
"commute crowding now, ED visits later" causal story to be mechanistically
plausible — incubation and care-seeking don't usually take two months. A more
likely explanation is a confounded second-order seasonal pattern (both
series move with the academic year/winter approach on a multi-week offset)
rather than a real leading-indicator relationship. Worth treating as
descriptive, not as evidence for the commute hypothesis specifically, until a
matched-baseline study (Phase 2) can separate the two.

**An open problem found while re-running this analysis:** redoing the
wastewater lag search above (same `lagged_cross_correlation` call, same
real data) no longer picks lag 0 as the best lag for Influenza A or RSV —
it now picks lag 4 (corr -0.45, vs. +0.44 at lag 0) and lag 7 respectively,
both close in magnitude but opposite in sign to the lag-0 result this
section's headline number is based on. This wasn't caused by the
transit/weather/bikeshare backfill (wastewater wasn't part of it) and isn't
a tail/reporting-lag artifact (it persists after trimming the most recent 12
weeks) — it appears the lag-0-vs-lag-4 pick for Influenza A is a genuinely
close call that flipped between whenever the table above was generated and
now, most likely because the underlying real-time WastewaterSCAN/MA DPH
values have been revised since. **Not yet resolved** — the wastewater numbers
above are left as previously reported pending a decision on how to handle
this instability (e.g. preferring lag 0 by hypothesis rather than by raw
`argmax(abs(corr))`, or reporting a sensitivity range instead of one lag).

**Events couldn't be tested at all, but the gap is now closing:**
Ticketmaster and Boston.gov civic events are *upcoming-events* APIs (rolling
~365 days forward), so any single day's `events` snapshot has zero date
overlap with historical `hospital_demand` — there is currently no way to
backtest the large-gatherings hypothesis against *event-level* data, only
against the academic-calendar population proxy above. `run.py` now also
maintains `events_archive.parquet`: each day's snapshot is folded into a
running history (deduplicated by date + event name) instead of being
overwritten, so real event-level overlap with `hospital_demand` accumulates
at roughly a year per year of daily runs. It can't backfill the past, so
event-level backtesting is still not possible *today* — but the archive is
the prerequisite Phase 2's matched-baseline event studies need, and the
sooner it starts accumulating the sooner that becomes possible.

---

## Known limitations

In the spirit of an honest status report, not just a feature list:

- **`hospital_demand` is respiratory-illness specific, not all-cause hospital
  demand.** The pipeline's only real source (`data/ma_dph_respiratory.csv`, MA
  DPH "broad acute respiratory" ED visits/admissions) and its automated
  fallback (CDC FluView ILI) both cover respiratory illness only. Predicting
  *overall* hospital/ED demand is the project's long-term goal, not its
  current state — every result in "What we've found so far" and every
  "hospital demand" label in the dashboard is a respiratory-demand proxy.
  Testing the broader hypothesis would need an all-cause ED-visit or
  admissions source added alongside this one.
- **`hospital_demand` likely conflates two demographically distinct
  populations with different drivers, and nothing in the pipeline can
  separate them.** CDC NHAMCS data shows ED visit rates by age are roughly
  U-shaped even *within* adults — 18–44: 47/100, 45–64: 43/100, 65–74: 47/100,
  75+: 66/100 (2019), with the 18–44 bump concentrated in ages 18–24
  (injury/mental-health/substance-use). The two humps differ in *how* people
  arrive and what happens next, not just in rate: ~6.7% of pediatric and
  ~14.5% of non-elderly-adult ED visits arrive by ambulance vs. ~43.2% for
  65+, and 53% of those elderly ambulance-transports end in admission. So a
  single weekly `hospital_demand` series is plausibly a superposition of a
  "young-adult" component (low-acuity, walk-in, maybe correlated with
  `events`/`academic_calendar`) and an "elderly" component (high-acuity,
  ambulance-heavy, maybe correlated with `weather`/`wastewater`) — pooling
  both in one regression could wash out a real effect in either. Neither
  `data/ma_dph_respiratory.csv` nor CDC FluView carry age breakdowns to test
  this. **Possible future exploration:** if an age-stratified
  `hospital_demand` source (or an elderly-skewed proxy like ambulance trip
  volume — see MATRIS/Health Care Capacity dashboard) becomes available, rerun
  `lagged_cross_correlation` / `fit_count_regression` per age band as a subset
  analysis to check whether driver relationships differ by population.
- **The MWRA wastewater fallback has never run against live data.** Its
  machine-readable export URL moves over time and `wastewater.mwra.data_url`
  has never been set, so that tier is unexercised code. WastewaterSCAN (Tier
  1) covers all four pathogens, so this is low-priority — but it's not the
  "tested fallback" the docstrings imply.
- **Second city is unbuilt.** The `CityDataProvider` abstraction is designed
  to make a second city "one YAML + one provider class," but that claim has
  never actually been tested against a real second city.
- **Bikeshare's GBFS fallback is a stock measure, not a flow measure.** The
  primary signal (`fetch_trip_history`) is a real daily ride count pulled from
  Bluebikes' public S3 trip-data archive, but monthly files are published with
  a ~1-2 month lag (e.g. fetching through December 2025 in mid-2026, the
  November 2025 file was still 404). The GBFS `station_status` fallback only
  reports bikes currently docked system-wide *right now* — a rough proxy for
  that gap, accumulated daily like the MBTA live-snapshot fallback, but not
  the same quantity as rides/day.
- **Live-API fetchers are tested against mocked payloads, not real endpoints,
  in development.** The sandbox's network allowlist returns 403 for most data
  hosts (Ticketmaster, MBTA ArcGIS, mass.gov, Open-Meteo), so `pytest` covers
  these with captured payloads and the daily GitHub Actions run (full network
  access) is the actual integration test — confirmed working: the `data`
  branch has ~1,950 rows of real MBTA gated-entry ridership and 827 real
  upcoming events (616 Ticketmaster + 229 Boston.gov civic events). Bluebikes'
  S3 trip-data archive *is* reachable from this sandbox, though (confirmed:
  ~185 days of real ride counts fetched directly in development).
- **Transit, weather, and bikeshare history is no longer capped at ~1
  year, and a one-time backfill has now run for all three.** `run.py` used
  to overwrite `transit.parquet` / `weather.parquet` with each day's fetch
  window; it now merges each fetch into the existing file in place
  (`src/ingestion/timeseries_archive.py`), so the daily rolling fetch
  accumulates permanently going forward. A `workflow_dispatch --start
  2018-07-01` run backfilled real history for all three: bikeshare and
  transit to 2018-07-01 (through ~Apr–May 2026, limited by each source's
  publication lag), weather to 2018-07-01 through the present (Open-Meteo has
  no publication lag). That's why "The other three sub-hypotheses, on real
  data" above now reports ~358–361 weeks for these three instead of 47–81.
  MBTA's gated-entry data actually goes back further (to 2014) and
  Open-Meteo's archive decades further still — neither has been pulled back
  that far, since 2018-07-01 already fully covers `hospital_demand`'s real
  history (MA DPH data starts 2019-06-30), so there's little marginal value
  in going earlier than the dependent variable itself.
- **Events have zero overlap with historical hospital demand (today) — but
  `events_archive.parquet` is now accumulating one.** Ticketmaster and
  Boston.gov civic events are upcoming-events APIs (rolling ~365 days
  forward), so the `events` signal still can't be backtested against past
  `hospital_demand` *yet* — only the academic-calendar population proxy can.
  As of this session, each day's snapshot is folded into a running archive
  instead of discarded, so real event-level overlap will build up at roughly
  a year per year of daily runs, which Phase 2's matched-baseline event
  studies will need.
- ~~**Boston.gov civic events appear to be contributing zero rows in
  production.**~~ **Fixed.** The fetcher was sending
  `sort=field_event_date_recur_value`, which Drupal's JSON:API rejects with
  `400 Bad Request` (confirmed in production logs) — `civic_events.fetch_events`
  failed soft to empty on every run. Removing the unsupported `sort` param (and
  sorting client-side instead) immediately recovered 229 civic events
  (marathons, parades, festivals, public health fairs) on the next run.

---

## Project status

| Component | Status |
|-----------|--------|
| Ingestion pipeline | Working — transit, weather, events, academic calendar, wastewater, hospital demand. All six signals have a sample-data fallback. |
| Daily GitHub Actions | Working — daily ingestion (`ingest.yml`) + PR test gate (`test.yml`) |
| Dashboard | Working — reads from data branch, no secrets needed; per-pathogen wastewater series, lagged regression panel |
| Cross-correlation analysis | Working — `src/analysis/correlate.py`, used by the dashboard |
| Count + surge regression | Working, in the dashboard — `src/analysis/regression.py` (Poisson/NB count models, surge-label logistic regression with AUC-ROC), tested against real data for all four sub-hypotheses |
| Test suite | 77 tests, all passing |
| Phase 2 event studies | Planned |
| Second city | Architecture ready, untested |

---

## License

MIT — see `LICENSE`.
