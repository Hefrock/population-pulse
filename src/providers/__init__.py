"""Provider registry.

Maps a city slug to its concrete :class:`CityDataProvider` subclass. To add a
city: implement the provider, then add one line here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.providers.base import CityDataProvider
from src.providers.boston import BostonProvider

# slug -> provider class
_REGISTRY: dict[str, type[CityDataProvider]] = {
    "boston": BostonProvider,
}


def load_provider(city_slug: str, config_dir: str | Path = "cities") -> CityDataProvider:
    """Return an initialized provider for ``city_slug``.

    Raises a clear error if the city is unknown or its config is missing.
    """
    if city_slug not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(f"Unknown city '{city_slug}'. Registered cities: {known}")

    config_path = Path(config_dir) / f"{city_slug}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found for '{city_slug}': {config_path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    return _REGISTRY[city_slug](config)


__all__ = ["CityDataProvider", "load_provider"]
