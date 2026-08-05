# CLAUDE.md

Working notes for Claude Code in this repo. Read this before making changes.

## What this is

**population-pulse** tests whether a city's population activity (events, weather,
disease, commute) predicts pressure on hospital emergency departments. Phase 1 is
descriptive: ingest signals → align on a weekly timeline → run lagged
cross-correlation against hospital demand. Boston is the only city, but the
architecture is deliberately city-agnostic.

**Current scope vs. the long-term goal:** the eventual aim is *overall* hospital
demand, but the only `hospital_demand` data available today is
**respiratory-illness** ED visits/admissions (MA DPH `ed_visits_respiratory` /
`hospital_admissions_respiratory`, or CDC FluView `ili_patients` as an automated
fallback — see "Data provenance" below). Every `hospital_demand` reference in this
codebase, the dashboard, and the README's results is therefore a respiratory-demand
proxy, not all-cause hospital demand — don't describe it as the latter.

## Commands

```bash
pip install -r requirements.txt              # deps
python -m src.ingestion.make_samples          # regenerate synthetic sample data
python -m src.ingestion.run --city boston     # full ingest (writes data/boston/*.parquet)
streamlit run src/dashboard/app.py            # dashboard
pytest tests/ -q                              # test suite (currently 149)
```

`run.py` accepts `--start`/`--end` (ISO dates); default is the trailing 365 days.

## Architecture (and the one rule)

**Pipeline code never knows which city it's looking at.** It only talks to a
`CityDataProvider` (`src/providers/base.py`). Adding a city = one YAML config +
one provider subclass; nothing else changes. Don't put Boston-specific facts
anywhere but `cities/boston.yaml` and `src/providers/boston.py`.

```
cities/boston.yaml         # the single place Boston-specific facts live
src/providers/base.py      # abstract CityDataProvider (one fetch_* per signal)
src/providers/boston.py    # concrete Boston provider — delegates to ingestion/
src/ingestion/*.py         # one fetcher per source; returns a tidy DataFrame
src/analysis/correlate.py  # align(), seasonal_residual(), lagged_cross_correlation(), driver_correlation_matrix(), scan_drivers()
src/analysis/regression.py # multi-driver lagged Poisson/NB regression + surge-label logistic regression (AUC-ROC); driver_vif(); walk_forward_validate_count()/walk_forward_validate_logistic()
src/analysis/multiple_comparisons.py  # Benjamini-Hochberg FDR correction + lag-selection ambiguity detection across a scan_drivers() family; summarize_scan()
src/dashboard/app.py       # Streamlit; reads Parquet, no API keys — uses correlate + regression + multiple_comparisons
src/ingestion/make_samples.py  # synthetic data with planted signals for offline/CI
src/ingestion/events_archive.py  # folds daily events snapshots into events_archive.parquet
src/ingestion/timeseries_archive.py  # merges each self-archiving signal's fetch into its parquet in place
src/ingestion/sample_window.py  # shift_sample_to_window() — shared helper that re-dates a bundled sample's timestamps into the requested window (weather/wastewater/hospital/mbta fallbacks)
```

### Signals (drivers + the dependent variable)

| Signal | Fetcher | Shape (cols beyond `timestamp`) |
|--------|---------|----------------------------------|
| transit | `mbta.py` | `route`, `value` |
| transit_service_level | `mbta.py` | `route`, `value` (live-vehicle stock, kept separate from `transit`'s flow — see below) |
| bikeshare | `bluebikes.py` | `value` |
| weather | `weather.py` | one column per variable (wide) |
| events | `events.py` + `ticketmaster.py` + `civic_events.py` | `venue`, `name`, `expected_attendance`, `source` |
| academic_calendar | `academic_calendar.py` | `school`, `value` |
| wastewater | `wastewater.py` | `pathogen`, `value`, `source` |
| hospital_demand | `hospital.py` + `cdc_fluview.py` | `metric`, `value` (respiratory-only, see above) |

`events` is also accumulated into `data/<city>/events_archive.parquet` by
`run.py` (each day's upcoming-events snapshot folded into a running history,
deduplicated by date + event name) — see `src/ingestion/events_archive.py` and
README's "Known limitations" for why this exists.

`transit`, `transit_service_level`, `weather`, `bikeshare`, `academic_calendar`,
`wastewater`, and `hospital_demand` are all merged into their existing
`data/<city>/*.parquet` in place by `run.py` (each fetch folded into the
accumulated file, deduped on a per-signal key — `timestamp`[, `route`/`school`/
`pathogen`/`metric`])
instead of overwriting it — see `src/ingestion/timeseries_archive.py` and
`run.py`'s `TIMESERIES_KEY_COLUMNS`. This removes the ~1-year rolling cap and
is the prerequisite for a real historical backfill (MBTA gated entries to
2014, Open-Meteo archive decades further). `bikeshare`'s GBFS fallback in
particular has *no* history of its own (a "right now" snapshot, like MBTA's
live-vehicle fallback), so this accumulation is how it builds one over time.
**Every signal whose fetcher already returns real historical data belongs in
`TIMESERIES_KEY_COLUMNS`** — `events` is the one legitimate exception (an
*upcoming-events* snapshot with its own `events_archive.parquet` mechanism
instead, see below). Forgetting to add a new self-archiving signal here is a
real bug, not a style nit: the daily cron always runs with the default
trailing-365-day window, so an un-merged signal gets silently overwritten
down to ~365 days on its very next scheduled run, discarding however much
history a backfill had just restored. This is exactly what happened to
`wastewater` and `hospital_demand` before they were added here — see
README's "What we've found so far" for the wastewater incident this
uncovered. The same "newer fetch wins" dedup tie-break also lets `wastewater`
absorb legitimate upstream revisions to already-published dates instead of
being stuck with a stale value forever.

`transit_service_level` is a useful edge case of this rule: it *does*
accumulate in `TIMESERIES_KEY_COLUMNS` like any other self-archiving signal,
but it must never be merged into `transit`'s own history — the two measure
different things (vehicles-in-service, a stock, vs. fare-gate taps, a flow),
and `align()` sums every row sharing a timestamp regardless of route, so
splicing a stock measure into a flow measure's column would corrupt the
composite even though their route-label vocabularies never literally
collide. See `mbta.py::fetch_transit_service_level`'s docstring for the full
reasoning — this is the general principle to apply to any future signal that
looks superficially similar to an existing one: accumulate flow-vs-stock (or
otherwise semantically distinct) measures as genuinely separate signals, not
as a fallback tier within an existing one.

`hospital_demand` is the **dependent variable**; everything else is a driver. It
currently represents respiratory-illness ED demand specifically, not all-cause
hospital demand.

See README's "Known limitations" for the current honest list of gaps (MWRA
wastewater fallback is unexercised, `transit`'s gated-entry source still has
a genuine 1-2 month publication lag that `transit_service_level` only
partially answers, events have zero overlap with historical hospital demand
yet, second city untested). Worth fixing opportunistically, but don't let
them block unrelated work.

## Conventions to follow

- **Timestamps are timezone-aware.** Fetchers localize to the city timezone;
  `align()` converts to UTC. Mind DST — there are regression tests for the
  spring-forward/fall-back edge cases (`tz_localize(..., nonexistent=..., ambiguous=...)`).
- **Fallback tiers, fail soft.** Every fetcher degrades gracefully: real source →
  (sometimes a second real source) → synthetic sample, returning a correctly-typed
  empty frame rather than crashing. One bad source must not kill the run
  (`run.py` wraps each fetch in try/except). Follow this pattern for new sources.
- **Remote schemas can shift.** For live APIs, auto-discover field names rather
  than hard-coding them (see `mbta.py`'s ArcGIS discovery and `wastewater._parse_cdc_nwss`'s
  long/wide handling). Pin field overrides in config only as an escape hatch.
- **`align()` sums each signal into one weekly series** (`resample("W").sum()`).
  Multi-row signals (transit routes, schools) are summed into a composite — that's
  intended. If sub-categories shouldn't be summed (e.g. wastewater pathogens on
  different scales), split them into separate signals *before* `align()`; the
  dashboard does this for wastewater and filters `hospital_demand` to one metric.
- **Correlation is Pearson** — scale- and shift-invariant — so absolute units
  (RNA copies vs. normalized activity levels, weekly sum vs. mean) don't affect
  the lag result. Don't add normalization the analysis doesn't need.
- **Synthetic samples carry planted signals** (winter surge, leading indicators)
  so the correlation code can be verified to recover known relationships. When you
  add a signal, add a `make_*` to `make_samples.py` with a deliberate relationship.
- **Deseasonalize via `correlate.seasonal_residual()`**, not ad hoc rolling
  means — it's the single shared definition of "elevated for the time of
  year" used by both `lagged_cross_correlation` and
  `regression.build_surge_labels`. Pass `causal=True` for any use that scores
  or predicts future weeks (walk-forward validation, anything that must not
  see data beyond "now") — it restricts the rolling seasonal mean to
  trailing-only data instead of a centered window, so no future information
  leaks into a supposedly out-of-sample score. Leave it `False` (the default)
  for descriptive/exploratory analysis over a fixed historical window, where
  a centered window is the more accurate seasonal estimate.
- **Count regressions default to `family="poisson"` but prefer
  `"negative_binomial"` for real data.** On real weekly ED-visit counts,
  Poisson p-values are wildly overconfident (overdispersion); NB is the
  better-specified model even though it needs more data to fit cleanly. See
  README's "What we've found so far" for a worked example.
- **Testing multiple drivers or multiple lags at once needs correction, not
  just the raw p-value.** `src/analysis/multiple_comparisons.py`'s
  `summarize_scan()` applies Benjamini-Hochberg FDR correction across a
  `scan_drivers()` family and flags a driver's best lag as ambiguous when its
  confidence interval overlaps the runner-up lag's — use it instead of
  reading `lagged_cross_correlation`'s raw p-value in isolation whenever more
  than one driver or more than one lag is being compared.
- **Recurring items that need a human to act (not a code fix) get a
  `[blocked-human]`-labeled GitHub issue, opened by a scheduled workflow that
  checks for an existing open one first** rather than relying on someone to
  remember. Two examples of this pattern today: the CHIA data-request issue
  (externally blocked) and `.github/workflows/academic-calendar-reminder.yml`
  (an annual internal reminder — `data/boston_academic_calendar.csv` has no
  API behind it and silently drifted 68 days stale before that workflow
  existed). Follow this pattern for any future "someone needs to periodically
  do a manual thing" gap rather than adding a comment nobody will see again.

## Data provenance

Curated reference data (`data/boston_events.csv`, `data/boston_academic_calendar.csv`)
is hand-maintained because no API exists. The academic calendar uses a `source`
column (`verified-YYYY-MM` / `estimate` / `prior-year-pattern`) so it's clear which
dates are sourced vs. inferred — preserve that honesty when editing. Note dates
mean: `start_date` = first day of classes, `end_date` = last day of final exams
(when students depart).

`data/ma_dph_respiratory.csv` (Tier 1 of `hospital.py`, statewide weekly ED
visits + admissions for "broad acute respiratory" diagnoses, 2019–present) is
also hand-maintained — regenerate it from a freshly downloaded MA DPH
"Respiratory Disease Reporting" workbook with `scripts/build_ma_dph_csv.py`
(see README). It's checked in like the other curated CSVs, not gitignored.

Sample-tier fallbacks for live-API fetchers (weather, wastewater, hospital,
mbta) share one helper, `sample_window.shift_sample_to_window()`, instead of
each fetcher hand-rolling its own date-shifting logic — it re-dates a bundled
sample's timestamps to line up with whatever window was actually requested,
so a stale bundled sample still looks plausible regardless of when the
pipeline runs. Use it for any new fetcher's sample fallback rather than
writing a new one.

## Environment caveat (important)

In the sandboxed/CI environment, **outbound network is restricted to an allowlist**
— most live data hosts (CDC, MWRA, MBTA ArcGIS, Open-Meteo, mass.gov) return 403
`host_not_allowed`, and GitHub is reachable. So:

- You usually **can't verify live fetchers against real endpoints here.** Test them
  against realistic captured payloads via monkeypatched `requests` (see the
  ticketmaster / civic_events / wastewater tests), and rely on the sample tier for
  end-to-end runs.
- `WebSearch` works even when `WebFetch` is blocked — use it to research/validate
  facts (e.g. how the academic calendar dates were checked).

## Tests & git

- Keep `pytest tests/ -q` green. New fetchers get: empty-frame schema, the
  parse/transform core (monkeypatched, no network), and an `align()` interaction
  test.
- `data/boston/*.parquet` are **gitignored but a few are tracked** from an earlier
  commit. Running the pipeline regenerates them as working-tree churn — `git
  checkout --` those before committing; don't commit regenerated parquet binaries.
- Sample CSVs under `data/samples/` **are** tracked — commit new ones.
- Develop on the assigned feature branch; commit with clear messages. Don't open a
  PR unless asked.
