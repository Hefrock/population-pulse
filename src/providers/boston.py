"""Boston implementation of :class:`CityDataProvider`.

Delegates to source-specific fetchers in ``src/ingestion``. Events are merged
from three sources in priority order: Ticketmaster, Boston.gov civic events,
manual CSV. MBTA automatically uses historical ridership first.
"""

from __future__ import annotations

import pandas as pd

from src.providers.base import CityDataProvider
from src.ingestion import (
    mbta, weather, events, hospital, ticketmaster, civic_events,
    academic_calendar, wastewater,
)


class BostonProvider(CityDataProvider):
    """Concrete provider for Boston, MA."""

    def fetch_transit(self, start: str, end: str) -> pd.DataFrame:
        cfg = self.config["transit"]
        return mbta.fetch_ridership(
            base_url=cfg["base_url"],
            routes=cfg["routes"],
            start=start,
            end=end,
            timezone=self.timezone,
            # Prefer historical gated-entry ridership; fall back to the live
            # snapshot (when MBTA_API_KEY is set) and then the bundled sample.
            historical=cfg.get("historical"),
            # live=None lets the fetcher auto-detect based on MBTA_API_KEY
        )

    def fetch_weather(self, start: str, end: str) -> pd.DataFrame:
        cfg = self.config["weather"]
        return weather.fetch_open_meteo(
            base_url=cfg["base_url"],
            latitude=self.config["city"]["latitude"],
            longitude=self.config["city"]["longitude"],
            variables=cfg["variables"],
            start=start,
            end=end,
            timezone=self.timezone,
        )

    def fetch_events(self, start: str, end: str) -> pd.DataFrame:
        cfg = self.config["events"]
        frames = []

        # 1. Ticketmaster — Sports and Music at major venues
        tm_cfg = cfg.get("ticketmaster", {})
        if tm_cfg:
            tm_df = ticketmaster.fetch_events(
                base_url=tm_cfg["base_url"],
                city=tm_cfg["city"],
                state_code=tm_cfg["state_code"],
                start=start,
                end=end,
                timezone=self.timezone,
                segments=tm_cfg.get("segments"),
            )
            if not tm_df.empty:
                frames.append(tm_df)

        # 2. Boston.gov civic events — marathons, parades, festivals, public events
        ce_cfg = cfg.get("civic_events", {})
        if ce_cfg:
            ce_df = civic_events.fetch_events(
                base_url=ce_cfg["base_url"],
                start=start,
                end=end,
                timezone=self.timezone,
            )
            if not ce_df.empty:
                frames.append(ce_df)

        # 3. Manual CSV — hand-curated baseline, always attempted
        csv_cfg = cfg.get("manual_csv", {})
        if csv_cfg:
            csv_df = events.fetch_manual_csv(
                path=csv_cfg["path"],
                start=start,
                end=end,
                timezone=self.timezone,
            )
            if not csv_df.empty:
                # Align schema to include source column for dedup
                csv_df = csv_df.copy()
                csv_df["source"] = "manual"
                frames.append(csv_df)

        cols = ["timestamp", "venue", "name", "expected_attendance", "source"]
        if not frames:
            return pd.DataFrame(columns=cols)

        combined = pd.concat(frames, ignore_index=True)

        # Deduplicate: same date + same event name from multiple sources
        combined["_date"] = combined["timestamp"].dt.date.astype(str)
        combined["_name_key"] = combined["name"].str.lower().str.strip()
        combined = combined.drop_duplicates(subset=["_date", "_name_key"], keep="first")
        combined = combined.drop(columns=["_date", "_name_key"])

        return combined[cols].reset_index(drop=True)

    def fetch_hospital_demand(self, start: str, end: str) -> pd.DataFrame:
        cfg = self.config["hospital_demand"]
        return hospital.fetch_ma_dph_respiratory(
            metrics=cfg["metrics"],
            start=start,
            end=end,
            timezone=self.timezone,
            state=cfg.get("state", "Massachusetts"),
        )

    def fetch_academic_calendar(self, start: str, end: str) -> pd.DataFrame:
        cfg = self.config["academic_calendar"]
        return academic_calendar.fetch_population_index(
            path=cfg["manual_csv"]["path"],
            start=start,
            end=end,
            timezone=self.timezone,
            ramp_days=cfg.get("ramp_days", academic_calendar.RAMP_DAYS),
        )

    def fetch_wastewater(self, start: str, end: str) -> pd.DataFrame:
        cfg = self.config["wastewater"]
        return wastewater.fetch_wastewater(
            pathogens=cfg["pathogens"],
            start=start,
            end=end,
            timezone=self.timezone,
            wastewaterscan=cfg.get("wastewaterscan"),
            mwra=cfg.get("mwra"),
            cdc_nwss=cfg.get("cdc_nwss"),
        )
