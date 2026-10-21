import json
from typing import Any
from uuid import uuid4

from data.quality.scoring import assess_quality
from domain.models import Evidence, now_ms


def build_evidence(
    *,
    tool: str,
    source: str,
    symbol: str,
    observed_at: int | None,
    ttl_ms: int,
    summary: dict[str, Any],
    data: Any = None,
    status: str = "ok",
    error: str | None = None,
) -> Evidence:
    at = now_ms()
    # Pydantic's Any payloads otherwise allow non-finite values through nested containers.
    json.dumps({"summary": summary, "data": data}, ensure_ascii=False, allow_nan=False)
    return Evidence(
        id="ev_" + uuid4().hex[:16],
        tool=tool,
        source=source,
        symbol=symbol,
        fetched_at=at,
        observed_at=observed_at,
        ttl_ms=ttl_ms,
        quality=assess_quality(source, observed_at, ttl_ms, status, at),
        summary=summary,
        data=data,
        status=status,
        error=error,
    )
