"""Feature validity with explicit provenance; invalid/stale values are withheld from consumers."""

import math

from domain.models import Evidence


def evidence_status(record: Evidence | None, at: int) -> str:
    if record is None or record.status == "unavailable":
        return "MISSING"
    if record.status == "error":
        return "INVALID"
    if record.observed_at is None:
        return "MISSING"
    if record.observed_at > at:
        return "INVALID"
    if at - record.observed_at > record.ttl_ms:
        return "STALE"
    if record.status == "partial":
        return "MISSING"
    return "VALID"


def missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(missing(v) for v in value.values())
    if isinstance(value, (tuple, list)):
        return any(missing(v) for v in value)
    return isinstance(value, float) and not math.isfinite(value)


class FeatureSet:
    def __init__(self, at: int):
        self.at = at
        self.values, self.status, self.metadata = {}, {}, {}

    def add(
        self,
        name: str,
        value,
        records=(),
        *,
        unit: str = "ratio",
        status: str | None = None,
        method: str = "",
    ):
        states = [evidence_status(r, self.at) for r in records]
        if status:
            states.append(status)
        if missing(value):
            states.append("MISSING")
        state = next((s for s in ("INVALID", "STALE", "MISSING") if s in states), "VALID")
        self.values[name] = value if state == "VALID" else None
        self.status[name] = state
        self.metadata[name] = {
            "evidence_ids": [r.id for r in records if r],
            "unit": unit,
            "as_of": self.at,
            "method": method,
        }
