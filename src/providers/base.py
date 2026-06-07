"""Abstract interface that every city must implement.

The whole point of this module is refactorability. Pipeline code (ingestion,
analysis, dashboard) only ever talks to a ``CityDataProvider`` — it never knows
whether it's looking at Boston or anywhere else. Adding a new city means writing
a new subclass, not editing the pipeline.

Each ``fetch_*`` method returns a tidy pandas DataFrame with, at minimum, a
timezone-aware ``timestamp`` column so that downstream alignment can resample
everything onto a common timeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


class CityDataProvider(ABC):
    """Base class for a city's data sources.

    Subclasses are constructed from a parsed config dict (see ``cities/*.yaml``)
    and expose one ``fetch_*`` method per signal. Methods should be safe to call
    repeatedly and should fail loudly with a clear message if a required secret
    (e.g. an API key) is missing.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.name: str = config["city"]["name"]
        self.slug: str = config["city"]["slug"]
        self.timezone: str = config["city"]["timezone"]

    # --- construction --------------------------------------------------------

    @classmethod
    def from_config_file(cls, path: str | Path) -> "CityDataProvider":
        """Load a YAML config and return the matching provider instance.

        Subclasses don't override this; the registry in ``__init__`` of the
        providers package maps a city slug to the right concrete class.
        """
        with open(path, "r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
        return cls(config)

    # --- population-flow signals --------------------------------------------

    @abstractmethod
    def fetch_transit(self, start: str, end: str) -> pd.DataFrame:
        """Transit ridership/volume between ``start`` and ``end`` (ISO dates).

        Returns columns: ``timestamp``, ``route``, ``value`` (a flow proxy).
        """

    @abstractmethod
    def fetch_weather(self, start: str, end: str) -> pd.DataFrame:
        """Hourly weather. Columns: ``timestamp`` plus configured variables."""

    @abstractmethod
    def fetch_events(self, start: str, end: str) -> pd.DataFrame:
        """Large gatherings. Columns: ``timestamp``, ``venue``, ``name``,
        ``expected_attendance`` (nullable)."""

    @abstractmethod
    def fetch_academic_calendar(self, start: str, end: str) -> pd.DataFrame:
        """Student population in the city. Columns: ``timestamp``, ``school``,
        ``value`` (estimated students physically present that day)."""

    @abstractmethod
    def fetch_wastewater(self, start: str, end: str) -> pd.DataFrame:
        """Wastewater viral surveillance — a leading indicator of demand.

        Columns: ``timestamp``, ``pathogen``, ``value``, ``source`` (long form,
        one row per pathogen per sample period)."""

    # --- hospital-demand signal (dependent variable) ------------------------

    @abstractmethod
    def fetch_hospital_demand(self, start: str, end: str) -> pd.DataFrame:
        """Hospital-demand proxy. Columns: ``timestamp``, ``metric``, ``value``.

        For most cities this will be weekly ED-visit syndromic data.
        """
