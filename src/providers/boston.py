"""Boston implementation of :class:`CityDataProvider`.

This class is deliberately thin: each method delegates to a source-specific
fetcher in ``src/ingestion``. That keeps the per-source HTTP/parsing logic
isolated and testable, while this class just maps the abstract interface onto
Boston's particular sources (MBTA, Open-Meteo, a manual events CSV, MA DPH).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.providers.base import CityDataProvider
from src.ingestion import mbta, weather, events, hospital


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
        return events.fetch_manual_csv(
            path=cfg["path"],
            start=start,
            end=end,
            timezone=self.timezone,
        )

    def fetch_hospital_demand(self, start: str, end: str) -> pd.DataFrame:
        cfg = self.config["hospital_demand"]
        return hospital.fetch_ma_dph_respiratory(
            metrics=cfg["metrics"],
            start=start,
            end=end,
            timezone=self.timezone,
        )
