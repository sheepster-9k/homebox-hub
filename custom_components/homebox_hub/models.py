"""Data models for the Homebox Hub integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class HomeBoxGroupStatistics:
    """Homebox group-level statistics."""

    total_items: int
    total_locations: int
    total_value: float


@dataclass(slots=True, frozen=True)
class HomeBoxItemSummary:
    """Summary of a Homebox item (from list / tag query)."""

    item_id: str
    name: str
    fields: list[dict[str, Any]] | None = None
    location_id: str | None = None
    location_name: str | None = None
