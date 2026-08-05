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

1. **Ingests eight signals daily** via GitHub Actions:
   - Transit volume (MBTA gated station entries — historical daily ridership, 2014–present)
   - Transit service level (MBTA live-vehicle snapshot, accumulated daily — a same-day complement to gated-entry volume's 1-2 month publication lag; see "Data sources")
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
| Transit | MBTA Gated Station Entries (MassDOT open data) | Daily, 2014–present, but published with a 1-2 month lag | None |
| Transit (fallback) | MBTA V3 API | Real-time snapshot | Free — `api-v3.mbta.com` |
| Transit service level | MBTA V3 API live-vehicle counts, accumulated daily into its own signal (never merged into `transit` — see `mbta.py::fetch_transit_service_level`) | Real-time snapshot, building its own history since introduction | Free — `api-v3.mbta.com` |
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

**On the academic calendar:** there is no API for university term dates, so `data/boston_academic_calendar.csv` is a hand-curated reference (the same "manual baseline" pattern as the events CSV). Term dates are validated against each school's registrar and carry a `source` column (`verified-*`, `estimate`, or `prior-year-pattern`); refresh it ~10 minutes once a year when schools publish their next calendar. `.github/workflows/academic-calendar-reminder.yml` opens a `[blocked-human]`-labeled GitHub issue every July so this refresh doesn't get forgotten between years — see `.github/academic-calendar-reminder-body.md` for the per-school checklist.

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
│   ├── analysis/        # timeline alignment, lagged correlation, count/surge
│   │                    #   regression, multiple-comparisons correction
│   └── dashboard/       # Streamlit app
├── tests/               # pytest suite (149 tests)
├── docs/                # narrated walkthrough
└── .github/workflows/   # daily ingestion, PR test gate, academic-calendar
                          #   annual refresh reminder
```

**Tech stack:** Python, pandas, scipy/statsmodels, Streamlit, Parquet, GitHub Actions.

---

## Interpreting results

Correlation here is suggestive, not causal. Transit ridership, ED visits, and disease all rise and fall with the seasons, so the analysis deseasonalizes by default before computing lags. Treat early results as leads to investigate, not conclusions.

Phase 2 (planned) will run matched-baseline event studies — comparing event days against similar non-event days — to tighten the estimates.

---

## What we've found so far

**tl;dr:** As of the latest full scan (8 drivers × lags 0–8, 424 aligned weekly
observations), **13 of 72 driver-lag tests survive Benjamini-Hochberg
correction for multiple comparisons** (18 are significant before correcting —
correction is doing real work here, not a formality). `wastewater: Influenza A`
is the cleanest surviving result — lag 4, corrected p≈0, and *not* flagged
ambiguous (its best lag is unambiguously better than the runner-up).
`transit`, `wastewater: SARS-CoV-2`, and `bikeshare` all also survive
correction, but each is flagged **ambiguous** — the confidence interval
around its best lag overlaps the runner-up lag's, so the specific lag number
shouldn't be trusted, only "this driver relates to hospital demand somehow."
`weather`, `academic_calendar`, `wastewater: RSV`, and the newer
`transit_service_level` signal do not survive correction. This full-scan,
corrected view (`src/analysis/multiple_comparisons.py`, exposed in the
dashboard's Driver correlation matrix) replaced an earlier practice of
reporting each driver's own best lag in isolation, which overstated how many
"significant" results there really were — see "The full cross-driver
picture" below for what changed and why. The dashboard also now supports
walk-forward (expanding-window) out-of-sample validation
(`src/analysis/regression.py`), so a driver's in-sample fit can be checked
against genuinely held-out future weeks rather than trusted at face value.

The older, driver-by-driver univariate results below (wastewater's lag-0
story, the transit/weather/bikeshare backfill) are still accurate as
described and are kept for the methodology lessons they carry — just read
them knowing the corrected full-scan numbers above are the current
higher-confidence picture. **Wastewater's
lag-0 finding (Influenza A/RSV, AUC 0.67–0.68, p<0.01) no longer reproduces
as the pipeline's automatic "best lag" pick** — `lagged_cross_correlation`
now selects lag 4 for Influenza A and lag 7 for RSV, neither of which is a
useful predictor (AUC 0.41/0.61, not significant). The signal at lag 0
specifically hasn't gone away — tested directly it's *stronger* than the
original headline (AUC 0.74/0.73, now also significant in the
Negative-Binomial count model, p=0.009/0.012) — but it loses the automatic
selection by a hair's-breadth margin in raw correlation (+0.44 vs. −0.45)
that a single upstream data revision was enough to flip. That's a real
finding about the fragility of `argmax(abs(corr))` lag-selection on close
calls, not a sign wastewater stopped mattering. A separate, genuine
accumulation bug (`wastewater`/`hospital_demand`/`academic_calendar` weren't
merging history like `transit`/`weather` already did, so the daily cron was
silently eroding both back to a ~365-day window every night) is also fixed
now — that stops future erosion, but doesn't undo the data revision already
baked into today's wastewater values. See "Update" below for the full
account. Academic calendar is unchanged (its real history wasn't part of
this backfill). Details and caveats below.

This is the honest result of running the pipeline end-to-end on ~3.5 years of
real data (WastewaterSCAN wastewater + MA DPH respiratory ED visits, Dec 2022 –
May 2026, ~180 weekly observations). `src/analysis/regression.py` turns each
week into a binary "surge" label (deseasonalized respiratory ED-visit residual
in the top quartile — i.e. running hot *for that time of year*) and fits a
logistic regression of each wastewater pathogen against it. Two different
lags matter here, for two different reasons:

| Pathogen | At lag 0 (forced) — AUC-ROC (p) | NB count-model p-value | Auto-selected "best lag" today | AUC-ROC (p) at that lag |
|---|---|---|---|---|
| Influenza A | 0.74 (p=0.0001) | 0.0092 | 4 weeks | 0.41 (p=0.48, not significant) |
| RSV | 0.73 (p<0.0001) | 0.0118 | 7 weeks | 0.61 (p=0.53, not significant) |
| SARS-CoV-2 | 0.63 (p=0.18, n.s.) | — | 2 weeks | 0.42 (p=0.95, not significant) |

**Reads as a real signal at lag 0 — but the pipeline's automatic lag
selection currently picks the wrong lag.** Same-week Influenza A and RSV
wastewater levels meaningfully separate "surge" from "normal" weeks both in
the surge-logistic AUC and, with the current data, in the Negative-Binomial
count model too. But `lagged_cross_correlation` picks the "best lag" purely
by the largest `|Pearson correlation|` across lags 0–8, and right now lag 4
(Influenza A, corr −0.448) and lag 7 (RSV, corr −0.150) edge out lag 0 (corr
+0.439 / +0.137) by a tiny margin in that one statistic, even though lag 0 is
clearly the more *predictive* lag by every other measure (AUC, both
p-values). That's a real limitation of `argmax(abs(corr))` as a
lag-selection rule on a close call, not evidence the relationship
disappeared — and it means the dashboard's auto-suggested lag for these two
pathogens is, right now, the less useful one. SARS-CoV-2 wastewater shows
nothing useful for surge prediction at any lag — a negative result, not a
bug, unchanged from before.

**A methodology lesson that's now baked into the code:** at lag 0, both
pathogens fit with a Poisson GLM (`fit_count_regression(..., family="poisson")`)
come out *highly* "significant" (p≈0) with an AIC an order of magnitude worse
than the Negative-Binomial fit (Influenza A: Poisson AIC 74,404 vs. NB 3,581;
RSV: 87,538 vs. 3,603), because weekly ED-visit counts are heavily
overdispersed and Poisson assumes variance = mean. Once that's corrected,
both pathogens are *still* significant in the Negative-Binomial fit at lag 0
(p=0.009, p=0.012) — but neither is at the lag the automatic selector
currently picks instead (p=0.38, p=0.49), the same lag-0-vs-auto-lag split as
above. Lesson: **don't trust a Poisson p-value on real syndromic count
data** — always compare against `family="negative_binomial"`, and don't
assume the automatically-selected lag is the most informative one to fit.

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

**Update — the accumulation bug is fixed, but the lag-0 finding still
doesn't auto-reproduce:** re-running the wastewater lag search above (same
`lagged_cross_correlation` call, same real data) stopped picking lag 0 as the
best lag for Influenza A or RSV — it picks lag 4 (corr −0.448, vs. +0.439 at
lag 0) and lag 7 (corr −0.150, vs. +0.137 at lag 0) respectively, both close
in magnitude but opposite in sign to the lag-0 result this section's original
headline number was based on. Ruled out first: it wasn't a search-boundary or
tail/reporting-lag artifact (persisted after trimming the most recent 12
weeks and after widening the search to 16 weeks). One real cause, now fixed:
`run.py` only merged `transit`/`weather`/`bikeshare` fetches into their
existing parquet in place — `wastewater` and `hospital_demand` were still
being *overwritten* by every run, including the daily cron, which always
runs with the default trailing-365-day window. GitHub Actions history
confirms 8 scheduled runs executed between the headline table being written
(Jun 10) and this discrepancy being investigated (Jun 18), each silently
re-clipping both files down to the trailing year. `wastewater`,
`hospital_demand`, and `academic_calendar` now accumulate via
`timeseries_archive.merge()` like the other signals do (see "Known
limitations"), so the next scheduled run won't silently erase years of
history again.

**What that fix does *not* do: restore the original Jun 10 wastewater
snapshot.** The wide `--start 2018-07-01` backfill on Jun 18 re-pulled
wastewater's values fresh from the live WastewaterSCAN feed rather than
recovering what was originally fetched on Jun 10 — and wastewater
surveillance values are routinely revised after first publication, so
today's archive reflects the revised values, not the original ones.
Re-checking after the bug fix (same code, same data, run again today)
confirms the lag-4/lag-7 pick is the current, live state, not a transient
blip the fix corrected: `argmax(abs(corr))` genuinely still selects lag 4
and lag 7 on today's data. The lag-0 relationship itself hasn't weakened —
tested directly it's stronger than before (see the table above) — so the
practical lesson is narrower than "the finding disappeared": a single scalar
`|correlation|` pick is not a robust substitute for checking nearby lags'
downstream predictive value (AUC, regression p-value) when two lags are this
close, and that's true for any driver, not just wastewater.

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

### The full cross-driver picture, with multiple-comparisons correction

Every number in the sections above tests one driver against respiratory ED
demand in isolation, scanning lags 0–8 and picking the best one — but running
that many tests and reporting only the best result inflates how many
"discoveries" look real (the classic multiple-comparisons problem), and none
of it controls for the drivers being correlated with *each other* (transit,
bikeshare, and academic calendar especially). `src/analysis/multiple_comparisons.py`
now runs the full driver × lag family through Benjamini-Hochberg
false-discovery-rate correction and flags each driver's best lag as
**ambiguous** whenever its confidence interval overlaps the runner-up lag's —
exposed in the dashboard's "Driver correlation matrix" view (Correlation &
Regression tab), computed live against whatever data is currently loaded.

As of this refresh: 13 of 72 driver-lag tests across 8 drivers survive
correction. `wastewater: Influenza A` is the strongest and least ambiguous
surviving result; `transit`, `wastewater: SARS-CoV-2`, and `bikeshare` survive
but with ambiguous best-lag selection; `weather`, `academic_calendar`,
`wastewater: RSV`, and `transit_service_level` do not survive. These numbers
will keep moving as more history accumulates — check the dashboard rather
than trusting this snapshot indefinitely.

**Walk-forward validation is the other half of the rigor story.** A driver
surviving correction on all available data still says nothing about whether
it would have predicted a surge *before it happened* — `regression.py`'s
`walk_forward_validate_count` / `walk_forward_validate_logistic` refit on an
expanding window and score only genuinely-held-out future weeks (using the
causal, trailing-only variant of `seasonal_residual` so no future data leaks
into deseasonalization). Available in the dashboard's Correlation & Regression
tab; not summarized here as a single number since, like the correlation
matrix, it's meant to be checked live rather than memorized from a doc that
will drift stale.

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
- **`transit`'s gated-entry source still has a genuine 1-2 month publication
  lag — that's MBTA's own documented cadence, not a bug this project can fix
  directly.** `transit_service_level` (MBTA's live-vehicle-count API,
  accumulated daily, kept as a deliberately separate signal — see
  `mbta.py::fetch_transit_service_level`) is a same-day complement, not a
  replacement: it measures vehicles in service (a stock), not fare-gate taps
  (a flow), and it's a new signal still building its own history, so it
  doesn't yet have `transit`'s multi-year depth. It does not currently
  survive multiple-comparisons correction in the full driver scan (see
  "What we've found so far") — too new, too little history, or both; worth
  re-checking as it accumulates.
- ~~**`wastewater`, `hospital_demand`, and `academic_calendar` had the same
  ~1-year-cap bug, and it silently erased the data behind the wastewater
  headline result.**~~ **Fixed.** Only `transit`/`weather`/`bikeshare` were
  ever added to `run.py`'s merge-on-fetch path — `wastewater` and
  `hospital_demand` (the dependent variable!) were still being overwritten
  outright by every run, including the **daily cron**, which always runs
  with the default trailing-365-day window. Confirmed in the Actions history:
  8 scheduled runs executed between the wastewater table being written
  (Jun 10) and the next time it was re-checked (Jun 18), each one silently
  re-clipping `wastewater.parquet`/`hospital_demand.parquet` down to the
  trailing year. The Jun 18 backfill happened to restore full history as a
  side effect (it fetches every signal with the same wide `--start`/`--end`),
  but that restoration re-pulled wastewater's historical values fresh from
  the live WastewaterSCAN feed rather than recovering whatever was
  originally fetched on Jun 10 — and wastewater surveillance values are
  routinely revised after publication. That combination (a `lagged_cross_correlation`
  pick that was already a near-tie — lag 0 at +0.44 vs. lag 4 at -0.45, see
  below — plus a guaranteed-different historical snapshot) is the resolved
  explanation for the lag flip. All three signals are now in `run.py`'s
  `TIMESERIES_KEY_COLUMNS` and accumulate the same way transit/weather/bikeshare
  do, so the *next* scheduled run won't repeat this. This fixes the
  *accumulation* bug going forward, but it can't undo the Jun 18 revision
  already baked into today's wastewater values — see "What we've found so
  far" for how that revision, not the bug, is what's currently flipping the
  wastewater lag pick.
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
| Ingestion pipeline | Working — transit, transit service level, weather, bikeshare, events, academic calendar, wastewater, hospital demand. All eight signals have a sample-data fallback. |
| Daily GitHub Actions | Working — daily ingestion (`ingest.yml`), PR test gate (`test.yml`), annual academic-calendar refresh reminder (`academic-calendar-reminder.yml`) |
| Dashboard | Working — reads from data branch, no secrets needed; per-pathogen wastewater series, lagged regression panel, driver correlation matrix, walk-forward validation |
| Cross-correlation analysis | Working — `src/analysis/correlate.py`, used by the dashboard |
| Multiple-comparisons correction | Working — `src/analysis/multiple_comparisons.py` (Benjamini-Hochberg FDR correction + lag-selection ambiguity detection across the full driver × lag family), used by the dashboard |
| Count + surge regression | Working, in the dashboard — `src/analysis/regression.py` (Poisson/NB count models, surge-label logistic regression with AUC-ROC, walk-forward out-of-sample validation), tested against real data for all four sub-hypotheses |
| Test suite | 149 tests, all passing |
| Phase 2 event studies | Planned |
| Second city | Architecture ready, untested |

---

## License

MIT — see `LICENSE`.
