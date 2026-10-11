"""Shared assessment wire schema, serialization and agent errors."""

import json

ASSESSMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["long", "short", "no_trade"]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "confidence", "rationale", "evidence_ids", "risks"],
}


class AgentError(RuntimeError):
    pass


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
