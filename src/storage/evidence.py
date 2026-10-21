import json
from pathlib import Path
from typing import Any

from data.assembly.evidence import build_evidence
from data.quality.scoring import assess_quality
from domain.models import Evidence


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
        evidence = build_evidence(
            tool=tool,
            source=source,
            symbol=symbol,
            observed_at=observed_at,
            ttl_ms=ttl_ms,
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
