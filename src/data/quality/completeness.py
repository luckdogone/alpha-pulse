from domain.models import Candle, Evidence


def recent_coverage_problems(rows: list[Candle], expected_minutes: int) -> list[str]:
    if sum(row.closed for row in rows) < expected_minutes:
        return ["missing candles in the recent half-hour window"]
    return []


def required_evidence(records: list[Evidence]) -> list[Evidence]:
    # The pipeline orders target candles, recent candles, book, flow, then derivatives.
    return records[:4] + [
        r for r in records[4:] if r.tool in {"get_mark_price", "get_open_interest"}
    ]


def required_evidence_problems(required: list[Evidence]) -> list[str]:
    problems = []
    if len(required) != 6:
        problems.append("required derivatives missing")
    for record in required:
        if record.status != "ok":
            problems.append(f"required data {record.tool}: {record.status}")
    return problems
