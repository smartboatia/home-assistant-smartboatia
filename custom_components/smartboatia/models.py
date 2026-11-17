"""Snapshot of the latest boat status, built from a decoded N-message."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .n_message import NMessage

# (measurement, variant, field); the variant is the instance number (battery, tanks,
# engine, transmission), the wind reference or the course orientation, else "".
ValueKey = tuple[str, str, str]


@dataclass
class BoatSnapshot:
    """Latest values. A key that is missing from ``values`` is stale or unknown."""

    timestamp: datetime | None = None
    origin: str | None = None
    rssi: int | None = None
    values: dict[ValueKey, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Plain representation for diagnostics."""
        return {
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "origin": self.origin,
            "rssi": self.rssi,
            "values": {"/".join(key): value for key, value in self.values.items()},
        }


def _variant(tags: dict[str, str]) -> str:
    for name in ("instance", "reference", "orientation"):
        if name in tags:
            return tags[name]
    return ""


def build_snapshot(message: NMessage) -> BoatSnapshot:
    """Turn a decoded message into a snapshot (samples tagged as outlier are skipped)."""
    values: dict[ValueKey, float] = {}
    for entry in message.entries:
        if "validity" in entry.tags:
            continue
        variant = _variant(entry.tags)
        for name, value in entry.fields.items():
            values[(entry.measurement, variant, name)] = value
    return BoatSnapshot(message.timestamp, message.origin, message.rssi, values)
