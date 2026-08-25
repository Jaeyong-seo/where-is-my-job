"""Deployment region for the automation subsystem.

The batch-policy timezone and the in-scope location set are deployment-wide
constants: SQL CHECK constraints bake the timezone into the database when
migrations are applied, and the migration checksums cover the substituted text,
so an existing database refuses to run under a different region (fail closed)
rather than silently reinterpreting its quotas.

Values come from the `search` block of `config/user-profile.json` when that
file exists; otherwise the historical Vancouver defaults apply, which keeps
behavior identical for unconfigured clones and CI. A malformed config raises
instead of falling back.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROFILE_PATH = _REPO_ROOT / "config" / "user-profile.json"

# The timezone is substituted into migration SQL, so it must be a strict IANA
# token — no quotes, spaces, or SQL metacharacters.
_TIMEZONE_TOKEN = re.compile(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}\Z")

DEFAULT_TIMEZONE = "America/Vancouver"
DEFAULT_CITY = "Vancouver"
DEFAULT_PROVINCE = "BC"
DEFAULT_COUNTRY = "Canada"
DEFAULT_LOCATIONS = (
    "Vancouver, BC", "Vancouver", "Metro Vancouver, BC", "Greater Vancouver, BC",
    "North Vancouver, BC", "West Vancouver, BC", "Burnaby, BC", "Richmond, BC",
)


@dataclass(frozen=True)
class Region:
    timezone: str
    city: str
    province: str
    country: str
    locations: frozenset[str]


def _required_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"region config {name} must be a non-empty string")
    return value.strip()


@lru_cache(maxsize=1)
def load_region() -> Region:
    timezone, city, province, country = DEFAULT_TIMEZONE, DEFAULT_CITY, DEFAULT_PROVINCE, DEFAULT_COUNTRY
    locations: tuple[str, ...] = DEFAULT_LOCATIONS
    if _PROFILE_PATH.exists():
        search = json.loads(_PROFILE_PATH.read_text(encoding="utf-8")).get("search", {})
        if not isinstance(search, dict):
            raise ValueError("config/user-profile.json: search must be an object")
        timezone = _required_str(search.get("timezone", timezone), "search.timezone")
        city = _required_str(search.get("target_city_label", city), "search.target_city_label")
        province = _required_str(search.get("province", province), "search.province")
        country = _required_str(search.get("country", country), "search.country")
        raw_locations = search.get("in_scope_locations", list(locations))
        if not isinstance(raw_locations, list) or not raw_locations:
            raise ValueError("config/user-profile.json: search.in_scope_locations must be a non-empty list")
        locations = tuple(_required_str(item, "search.in_scope_locations[]") for item in raw_locations)
    if not _TIMEZONE_TOKEN.fullmatch(timezone):
        raise ValueError(f"region timezone is not a strict IANA token: {timezone!r}")
    ZoneInfo(timezone)  # raises for unknown zones
    return Region(
        timezone=timezone, city=city, province=province, country=country,
        locations=frozenset(locations),
    )
