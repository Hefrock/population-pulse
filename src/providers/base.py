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

    def fetch_transit_service_level(self, start: str, end: str) -> pd.DataFrame:
        """Optional: live transit *service level* (vehicles currently in
        service), a stock measure distinct from ``fetch_transit``'s ridership
        flow measure.

        Not every city has an equivalent live-snapshot source, so this has a
        concrete no-op default (empty frame) rather than being abstract —
        override it only if the city's transit fetcher has a live-snapshot
        fallback worth accumulating as its own signal (see
        ``src/providers/boston.py``). It must stay a genuinely separate
        signal from ``fetch_transit``, never merged into its history:
        ``align()`` sums every row sharing a timestamp regardless of route,
        so splicing a stock measure into a flow measure's accumulated column
        would corrupt the composite's meaning.

        Returns columns: ``timestamp``, ``route``, ``value``.
        """
        return pd.DataFrame(columns=["timestamp", "route", "value"])

    @abstractmethod
    def fetch_bikeshare(self, start: str, end: str) -> pd.DataFrame:
        """Bike-share activity between ``start`` and ``end`` (ISO dates).

        Returns columns: ``timestamp``, ``value`` (daily ride count, a
        commute/mobility flow proxy alongside transit).
        """

    @abstractmethod
    def fetch_weather(self, start: str, end: str) -> pd.DataFrame:
        """Hourly weather. Columns: ``timestamp`` plus configured variables."""

    @abstractmethod
    def fetch_events(self, start: str, end: str) -> pd.DataFrame:
        """Large gatherings. Columns: ``timestamp``, ``venue``, ``name``,
        ``expected_attendance`` (nullable), ``source`` (which fetcher the row
        came from -- used to fold daily snapshots into ``events_archive``)."""

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
