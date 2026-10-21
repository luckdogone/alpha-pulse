from data.processing.indicators import calculate
from data.quality.completeness import (
    recent_coverage_problems,
    required_evidence,
    required_evidence_problems,
)
from data.quality.freshness import candle_freshness_problems
from domain.models import INTERVAL_MS, Candle, Evidence, Snapshot


def build_snapshot(
    info: dict, interval: str, end: int, records: list[Evidence], snapshot_minutes: int
) -> Snapshot:
    target, recent, book, flow = records[:4]
    rows = [Candle.model_validate(r) for r in (target.data or {}).get("candles", [])]
    recent_rows = [Candle.model_validate(r) for r in (recent.data or {}).get("candles", [])]
    problems = []
    try:
        indicators = calculate(rows)
    except ValueError as exc:
        indicators = {}
        problems.append(str(exc))
    problems.extend(recent_coverage_problems(recent_rows, snapshot_minutes))
    problems.extend(candle_freshness_problems(rows, recent_rows, end, INTERVAL_MS[interval]))
    required = required_evidence(records)
    problems.extend(required_evidence_problems(required))
    return Snapshot(
        **info,
        interval=interval,
        as_of=end,
        candles=rows,
        recent_1m=recent_rows,
        indicators=indicators,
        order_book=book.summary,
        order_flow=flow.summary,
        derivatives={r.tool.removeprefix("get_"): r.summary for r in records[4:]},
        evidence_ids=[r.id for r in records],
        required_ids=[r.id for r in required],
        problems=problems,
    )
