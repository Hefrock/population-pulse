# Getting Started — Phase 1

This is the first chapter of a tutorial that grows with the project. By the end
you'll have the pipeline running end-to-end on sample data, you'll understand
why each piece exists, and you'll know exactly what to swap in to use real data.

Each later phase appends its own chapter rather than rewriting this one, so you
can always retrace how the project evolved.

---

## 1. The question, restated

We want to know whether **surges in a city's population — from events, weather,
and disease — line up with increased demand on hospital emergency departments.**

The word "surges" is doing a lot of work. We can't directly count "how many
people are in Boston right now," so we use *proxies* for population flow (transit
ridership, traffic, event schedules) and correlate them against a *proxy* for
hospital demand (weekly ED-visit data). Phase 1 is about getting those proxies
onto one timeline so we can look at them together.

## 2. The single most important constraint

Before any code: **real-time, hospital-specific bed data is not public.** The
best a non-public-health user can get is *weekly* ED-visit syndromic data from
Massachusetts DPH. Everything about the design follows from that one fact:

- Our analysis resolution is **weekly**, not minute-by-minute.
- Minute-level signals (transit) get **aggregated up** to weekly.
- Single-night events may **wash out** in weekly numbers — which is why we test
  weather and disease (big, sustained effects) before large gatherings.

If you ever get access to CDC's NSSP/ESSENCE feed (restricted to public-health
jurisdictions), you can drop to daily resolution and the project gets much
sharper. Until then, weekly is the honest ceiling.

## 3. Set up

```bash
git clone <your-repo-url> population-pulse
cd population-pulse
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Generate the synthetic sample data so everything runs offline:

```bash
python -m src.ingestion.make_samples
```

This writes *fake but realistic* data with a planted winter respiratory surge
and a summer heat spike. We use it to confirm the analysis code can detect
relationships we know are there before trusting it on real data.

## 4. Run the pipeline

```bash
python -m src.ingestion.run --city boston --start 2024-06-01 --end 2025-05-31
```

You'll see a per-signal summary. Under the hood, `run.py` asked the **Boston
provider** for each signal, and the provider delegated to a **fetcher** per
source. That indirection is the key design choice — see §6.

## 5. Explore it

```bash
streamlit run src/dashboard/app.py
```

The dashboard shows every signal on a shared weekly timeline and lets you run a
**lagged cross-correlation** between any driver and hospital demand. Try
correlating `transit` against `hospital_demand` with deseasonalizing on, then
off, and watch how much the apparent relationship changes. That difference *is*
the lesson of §7.

## 6. Why the architecture looks the way it does

You'll notice a `CityDataProvider` base class and a `BostonProvider` subclass.
That feels like extra ceremony for one city — but you told us you'll want other
cities later. By isolating every Boston-specific fact (which API, which station
codes, which dashboard) behind a common interface now, adding Chicago later
means writing *one new provider*, not editing the pipeline, the analysis, or the
dashboard. The cost is a little structure today; the payoff is not rewriting
everything in three months.

The flow is:

```
run.py / dashboard
      │  (only ever calls the abstract interface)
      ▼
CityDataProvider  ◄── cities/boston.yaml  (all Boston-specific config)
      │
      ▼
BostonProvider
      │  (delegates per source)
      ▼
ingestion/{mbta, weather, bluebikes, events, ticketmaster, civic_events,
           academic_calendar, wastewater, hospital, cdc_fluview}.py
```

## 7. The trap you must not fall into

Transit ridership, ED visits, and disease **all rise in winter**. So if you
correlate raw transit against raw ED visits, you'll get a strong positive number
that means almost nothing — they're both just following the calendar.

This is why `lagged_cross_correlation` **deseasonalizes by default**: it strips a
rolling seasonal mean from both series before comparing them. It's also why we
scan a *range of lags* rather than just lag 0 — real effects are delayed
(disease incubation is up to ~2 weeks). Even with these guards, Phase 1
correlation is **suggestive, not causal**. Phase 2 introduces matched-baseline
event studies, which are much stronger evidence.

## 8. Going from sample data to real data

Each fetcher is honest about what it does:

- **Weather** (`ingestion/weather.py`) — *fully working now.* Open-Meteo needs
  no key; real historical weather is one function call away.
- **Transit** (`ingestion/mbta.py`) — the primary `transit` signal is
  historical MBTA gated-station-entry data, no key needed, but it's published
  with a 1-2 month lag. `fetch_live_vehicle_counts` also has a *working live
  call* against the real MBTA API — get a free key at https://api-v3.mbta.com/,
  put it in `.env`, and the scheduled Action will start accumulating it as a
  **separate** `transit_service_level` signal (same-day, but a different
  quantity — vehicles in service, not fare-gate taps — so it's deliberately
  never merged into `transit`'s own history; see `mbta.py::fetch_transit_service_level`).
- **Events** (`ingestion/events.py`) — reads `data/boston_events.csv`. Just edit
  that CSV with real dates; it's already wired in.
- **Hospital demand** (`ingestion/hospital.py`) — download the weekly file from
  the [MA DPH respiratory dashboard](https://www.mass.gov/info-details/weekly-flu-report)
  into `data/ma_dph_respiratory.csv` and it takes over from the sample
  automatically.

Swap them in one at a time. The pipeline keeps running even if a source is
missing, so you're never blocked.

## 9. What's next (Phase 2 preview)

Since this chapter was written, the project already picked up a chunk of
Phase 1's promised rigor: `lagged_cross_correlation` now reports confidence
intervals and p-values, `src/analysis/multiple_comparisons.py` corrects for
testing many drivers/lags at once (and flags ambiguous lag picks), and
`regression.py` supports walk-forward out-of-sample validation. The MA DPH
weekly download stayed manual on purpose — see README's "Data sources" for
why — but got provisional-tail handling instead so a partial latest week
doesn't get mistaken for a real drop.

What's still ahead:

- **Event-study analysis**: compare ED demand on event days against matched
  non-event baseline days, per sub-hypothesis — the real answer to "is this
  correlation causal," which lagged correlation alone can't give you.
- A second city, to prove out the `CityDataProvider` abstraction against
  more than just Boston.

That chapter will land as `docs/02-event-studies.md` when we build it.
