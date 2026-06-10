"""Convert a manually-downloaded MA DPH "Respiratory Disease Reporting" Excel
workbook into the ``timestamp,metric,value`` CSV that ``hospital.py``'s Tier 1
(``CACHED_PATH``) reads.

Source: https://www.mass.gov/info-details/weekly-flu-report — download the
current "Respiratory Disease Reporting" workbook (it includes all prior
seasons in its "Visits by week" sheet, so a single file is usually enough).

Usage:
    python scripts/build_ma_dph_csv.py path/to/RespiratoryDiseaseReporting*.xlsx [more files...]

Multiple files can be passed (e.g. an older archive plus the current-season
file); rows are concatenated and de-duplicated by (timestamp, metric),
preferring the *last* file listed for any overlapping weeks — pass the most
up-to-date file last.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

OUT_PATH = Path("data/ma_dph_respiratory.csv")

# "Visits by week" Visit type -> our metric name (cities/boston.yaml hospital_demand.metrics)
_VISIT_TYPE_TO_METRIC = {
    "ED visits": "ed_visits_respiratory",
    "Admissions": "hospital_admissions_respiratory",
}


def _extract(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="Visits by week")
    df = df[(df["Group"] == "Statewide") & (df["Subgroup"] == "Statewide")]
    df = df[df["Visit type"].isin(_VISIT_TYPE_TO_METRIC)]

    return pd.DataFrame({
        "timestamp": pd.to_datetime(df["Week Start Date"]).dt.strftime("%Y-%m-%d"),
        "metric": df["Visit type"].map(_VISIT_TYPE_TO_METRIC),
        "value": df["Number of broad acute respiratory visits"].astype(int),
    })


def main(paths: list[str]) -> None:
    if not paths:
        raise SystemExit("Usage: python scripts/build_ma_dph_csv.py <workbook.xlsx> [...]")

    frames = [_extract(Path(p)) for p in paths]
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["timestamp", "metric"], keep="last")
    combined = combined.sort_values(["metric", "timestamp"]).reset_index(drop=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(OUT_PATH, index=False)
    print(f"Wrote {len(combined)} rows to {OUT_PATH}")
    for metric, group in combined.groupby("metric"):
        print(f"  {metric}: {len(group)} weeks, {group['timestamp'].min()} -> {group['timestamp'].max()}")


if __name__ == "__main__":
    main(sys.argv[1:])
