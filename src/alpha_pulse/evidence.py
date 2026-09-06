import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import Evidence, Quality, now_ms


def assess_quality(
    source: str, observed_at: int | None, ttl_ms: int, status: str, at: int | None = None
) -> Quality:
    at = at or now_ms()
    if status in {"unavailable", "error"}:
        return Quality(score=0, level="unavailable", reasons=[status])
    score = {
        "binance": 0.95,
        "derived_binance": 0.9,
        "coinglass": 0.8,
        "coinmetrics": 0.8,
        "gdelt": 0.65,
        "configured_onchain": 0.5,
        "synthetic": 0.0,
    }.get(source, 0.4)
    reasons = [f"source={source}"]
    if observed_at is None:
        score *= 0.5
        reasons.append("provider observation timestamp unknown")
    elif observed_at > at + 5000:
        score = 0
        reasons.append("observation timestamp is in the future")
    elif at - observed_at > ttl_ms:
        score *= 0.25
        reasons.append("stale for this data type")
    else:
        reasons.append("within data-specific freshness window")
    if status == "partial":
        score *= 0.6
        reasons.append("partial coverage")
    return Quality(
        score=round(score, 3),
        level="high" if score >= 0.8 else ("medium" if score >= 0.5 else "low"),
        reasons=reasons,
    )


class EvidenceStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory
        self.records: dict[str, Evidence] = {}
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    def add(
        self,
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
        evidence = Evidence(
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
        self.records[evidence.id] = evidence
        if self.directory:
            (self.directory / f"{evidence.id}.json").write_text(
                evidence.model_dump_json(indent=2), encoding="utf-8"
            )
        return evidence

    def ledger(self, at: int | None = None) -> list[dict[str, Any]]:
        out = []
        for evidence in self.records.values():
            item = evidence.compact()
            item["quality"] = assess_quality(
                evidence.source, evidence.observed_at, evidence.ttl_ms, evidence.status, at
            ).model_dump()
            out.append(item)
        return out

    def page(
        self, evidence_id: str, offset: int = 0, limit: int = 20, field: str | None = None
    ) -> dict:
        evidence = self.records[evidence_id]
        data = evidence.data
        if field:
            for part in field.split("."):
                if not isinstance(data, dict) or part not in data:
                    raise ValueError("unknown evidence object field")
                data = data[part]
        if isinstance(data, dict) and isinstance(data.get("candles"), list):
            data = data["candles"]
        if isinstance(data, list):
            return {
                "evidence_id": evidence_id,
                "total": len(data),
                "offset": offset,
                "next_offset": offset + limit if offset + limit < len(data) else None,
                "data": data[offset : offset + limit],
            }
        if len(json.dumps(data, ensure_ascii=False)) > 8000:
            return {
                "evidence_id": evidence_id,
                "summary": evidence.summary,
                "fields": list(data) if isinstance(data, dict) else [],
                "note": "large object archived; select a field (e.g. trades or bids) to page it",
            }
        return {"evidence_id": evidence_id, "data": data}
