"""Wastewater viral-surveillance fetcher (disease leading indicator).

Concentrations of respiratory viruses shed in stool show up in municipal
wastewater days *before* the matching surge in clinical visits and hospital
admissions — typically a 4–10 day lead. That makes wastewater the single
strongest *leading* signal for the project's "disease" sub-hypothesis: unlike
the hospital series (the dependent variable), it is an independent driver that
should *precede* demand, so a positive lead lag in the cross-correlation is the
result we'd expect to see.

Multi-pathogen by design. Boston-area public data covers three respiratory
viruses, so the output carries a ``pathogen`` dimension and the dashboard
correlates each one against hospital demand separately (summing different
viral scales into one number would be meaningless).

Three-tier fallback, mirroring the rest of the pipeline:
  1. MWRA Deer Island (Biobot)  — metro-Boston SARS-CoV-2, the local gold
     standard (~2.3M-person sewershed, North + South systems).
  2. CDC NWSS "Wastewater Viral Activity Level" — SARS-CoV-2, Influenza A and
     RSV for Massachusetts statewide, via the data.cdc.gov Socrata API (no key).
     Fills in the pathogens MWRA's COVID-only feed doesn't cover.
  3. Bundled synthetic sample   — offline / CI only, with a planted lead.

Output (long form): ``timestamp``, ``pathogen``, ``value``, ``source``.

Both live sources are written defensively (auto-discovering field names and
failing gracefully) because their schemas can shift and neither is reachable
from every environment; when both are unreachable the pipeline still runs on
the sample, exactly like the transit and hospital fetchers.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

REQUEST_TIMEOUT = 30
SAMPLE_PATH = Path("data/samples/wastewater_sample.csv")

# Canonical pathogen labels. Keys are lowercase substrings we may see in a
# remote feed's column names or values; values are the label we emit.
_PATHOGEN_ALIASES = {
    "sars": "SARS-CoV-2",
    "cov": "SARS-CoV-2",
    "covid": "SARS-CoV-2",
    "influenza a": "Influenza A",
    "flu a": "Influenza A",
    "flua": "Influenza A",
    "influenza": "Influenza A",
    "rsv": "RSV",
    "respiratory syncytial": "RSV",
}


def fetch_wastewater(
    pathogens: list[str],
    start: str,
    end: str,
    timezone: str,
    mwra: dict | None = None,
    cdc_nwss: dict | None = None,
) -> pd.DataFrame:
    """Return a long-form wastewater series for the requested ``pathogens``.

    Columns: ``timestamp``, ``pathogen``, ``value``, ``source``. Prefers the
    metro-Boston MWRA feed for SARS-CoV-2 and CDC NWSS for the rest; falls back
    to the bundled sample when no live source is reachable.
    """
    wanted = [_canonical_pathogen(p) or p for p in pathogens]
    frames: list[pd.DataFrame] = []
    covered: set[str] = set()

    # Tier 1: MWRA Deer Island — metro-Boston SARS-CoV-2 only.
    if mwra and "SARS-CoV-2" in wanted:
        mwra_df = _fetch_mwra(mwra, start, end, timezone)
        if not mwra_df.empty:
            frames.append(mwra_df)
            covered.update(mwra_df["pathogen"].unique())

    # Tier 2: CDC NWSS — fill in whatever pathogens MWRA didn't cover.
    remaining = [p for p in wanted if p not in covered]
    if cdc_nwss and remaining:
        cdc_df = _fetch_cdc_nwss(cdc_nwss, remaining, start, end, timezone)
        if not cdc_df.empty:
            frames.append(cdc_df)
            covered.update(cdc_df["pathogen"].unique())

    if frames:
        out = pd.concat(frames, ignore_index=True)
        missing = [p for p in wanted if p not in covered]
        if missing:
            print(f"[wastewater] No live data for {missing}; reporting the rest.")
        return out.sort_values(["pathogen", "timestamp"]).reset_index(drop=True)

    # Tier 3: synthetic sample — offline / CI only.
    print(
        "\n⚠️  WARNING: [wastewater] Falling back to SYNTHETIC sample data.\n"
        "   Correlations computed with this data are not meaningful.\n"
        "   To use real data, ensure the MWRA or CDC NWSS sources are reachable.\n"
    )
    return _load_sample(wanted, start, end, timezone)


# --- canonicalization --------------------------------------------------------

def _canonical_pathogen(raw: str) -> str | None:
    """Map a free-text pathogen name/column to a canonical label, or None."""
    text = str(raw).strip().lower()
    if text in _PATHOGEN_ALIASES:
        return _PATHOGEN_ALIASES[text]
    # Longest alias first so "influenza a" wins over "influenza".
    for alias in sorted(_PATHOGEN_ALIASES, key=len, reverse=True):
        if alias in text:
            return _PATHOGEN_ALIASES[alias]
    return None


# --- Tier 1: MWRA / Biobot ---------------------------------------------------

def _fetch_mwra(cfg: dict, start: str, end: str, timezone: str) -> pd.DataFrame:
    """Best-effort metro-Boston SARS-CoV-2 from the MWRA Biobot feed.

    MWRA publishes the Deer Island series; the exact machine-readable URL has
    moved over time, so this reads whatever CSV ``data_url`` points at and
    auto-discovers the date and concentration columns. Any failure returns an
    empty frame so CDC NWSS (or the sample) takes over.
    """
    cols = ["timestamp", "pathogen", "value", "source"]
    url = cfg.get("data_url")
    if not url:
        return pd.DataFrame(columns=cols)
    try:
        raw = pd.read_csv(url)
    except Exception as exc:  # noqa: BLE001 — degrade to the next tier
        print(f"[wastewater] MWRA feed unavailable ({exc}); trying CDC NWSS.")
        return pd.DataFrame(columns=cols)

    date_col = _first_matching(raw.columns, ["date", "sample", "week"])
    value_col = _first_matching(
        raw.columns, ["copies", "concentration", "rna", "viral", "value"]
    )
    if date_col is None or value_col is None:
        print("[wastewater] Could not identify MWRA date/value columns; skipping.")
        return pd.DataFrame(columns=cols)

    out = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(raw[date_col], errors="coerce"),
            "value": pd.to_numeric(raw[value_col], errors="coerce"),
        }
    ).dropna()
    out["pathogen"] = "SARS-CoV-2"
    out["source"] = "mwra"
    return _localize_and_window(out, start, end, timezone)


# --- Tier 2: CDC NWSS (Socrata) ----------------------------------------------

def _fetch_cdc_nwss(
    cfg: dict, pathogens: list[str], start: str, end: str, timezone: str
) -> pd.DataFrame:
    """Massachusetts wastewater viral activity levels from the CDC NWSS API."""
    cols = ["timestamp", "pathogen", "value", "source"]
    base_url = cfg.get("base_url")
    if not base_url:
        return pd.DataFrame(columns=cols)
    state = cfg.get("state", "Massachusetts")
    try:
        resp = requests.get(base_url, params={"$limit": 50000}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        records = resp.json()
    except Exception as exc:  # noqa: BLE001 — degrade to the sample tier
        print(f"[wastewater] CDC NWSS unavailable ({exc}); using sample if needed.")
        return pd.DataFrame(columns=cols)

    return _parse_cdc_nwss(records, pathogens, state, start, end, timezone)


def _parse_cdc_nwss(
    records: list[dict],
    pathogens: list[str],
    state: str,
    start: str,
    end: str,
    timezone: str,
) -> pd.DataFrame:
    """Normalize CDC NWSS Socrata records into the long-form schema.

    Handles either layout the dataset might use: a long form with a pathogen
    column, or a wide form with one value column per pathogen. Kept pure (no
    network) so it can be unit-tested against a captured payload.
    """
    cols = ["timestamp", "pathogen", "value", "source"]
    if not records:
        return pd.DataFrame(columns=cols)

    keys = list(records[0].keys())
    date_key = _first_matching(keys, ["week_end", "date_end", "date", "week"])
    state_key = _first_matching(keys, ["state", "jurisdiction", "geography", "region"])
    pathogen_key = _first_matching(keys, ["pathogen", "target", "virus", "analyte"])
    if date_key is None:
        return pd.DataFrame(columns=cols)

    rows = []
    if pathogen_key is not None:
        # Long form: one row per (week, pathogen).
        value_key = _first_matching(
            keys, ["wval", "activity", "level", "percentile", "value"]
        )
        if value_key is None:
            return pd.DataFrame(columns=cols)
        for rec in records:
            if state_key and not _state_matches(rec.get(state_key), state):
                continue
            canon = _canonical_pathogen(rec.get(pathogen_key, ""))
            if canon is None or canon not in pathogens:
                continue
            rows.append((rec.get(date_key), canon, rec.get(value_key)))
    else:
        # Wide form: one column per pathogen.
        pathogen_cols = {}
        for key in keys:
            canon = _canonical_pathogen(key)
            if canon and canon in pathogens:
                pathogen_cols[key] = canon
        for rec in records:
            if state_key and not _state_matches(rec.get(state_key), state):
                continue
            for key, canon in pathogen_cols.items():
                rows.append((rec.get(date_key), canon, rec.get(key)))

    if not rows:
        return pd.DataFrame(columns=cols)

    out = pd.DataFrame(rows, columns=["timestamp", "pathogen", "value"])
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out.dropna(subset=["timestamp", "value"])
    out["source"] = "cdc_nwss"
    return _localize_and_window(out, start, end, timezone)


# --- Tier 3: sample ----------------------------------------------------------

def _load_sample(
    pathogens: list[str], start: str, end: str, timezone: str
) -> pd.DataFrame:
    cols = ["timestamp", "pathogen", "value", "source"]
    if not SAMPLE_PATH.exists():
        print(f"[wastewater] No sample at {SAMPLE_PATH}; continuing with no signal.")
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(SAMPLE_PATH, parse_dates=["timestamp"])
    df = df[df["pathogen"].isin(pathogens)].copy()
    if "source" not in df.columns:
        df["source"] = "sample"
    return _localize_and_window(df, start, end, timezone)


# --- shared helpers ----------------------------------------------------------

def _first_matching(columns, needles: list[str]) -> str | None:
    """First column whose lowercased name contains any of ``needles``."""
    for col in columns:
        low = str(col).lower()
        if any(n in low for n in needles):
            return col
    return None


def _state_matches(value, state: str) -> bool:
    """True if a record's state field matches (full name or 2-letter code)."""
    if value is None:
        return False
    v = str(value).strip().lower()
    s = state.strip().lower()
    return v == s or v == _STATE_ABBR.get(s, s)


_STATE_ABBR = {"massachusetts": "ma"}


def _localize_and_window(
    df: pd.DataFrame, start: str, end: str, timezone: str
) -> pd.DataFrame:
    """Localize naive timestamps and clip to [start, end]; tidy column order."""
    cols = ["timestamp", "pathogen", "value", "source"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    out = df.copy()
    if out["timestamp"].dt.tz is None:
        out["timestamp"] = out["timestamp"].dt.tz_localize(timezone)
    mask = (out["timestamp"] >= pd.Timestamp(start, tz=timezone)) & (
        out["timestamp"] <= pd.Timestamp(end, tz=timezone)
    )
    return out.loc[mask, cols].reset_index(drop=True)
