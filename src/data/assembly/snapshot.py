from data.assembly.features import assemble_features
from data.catalog import METRICS
from data.cleaning.candles import find_gaps
from data.processing.indicators import calculate
from data.quality.completeness import (
    recent_coverage_problems,
    required_evidence,
    required_evidence_problems,
)
from data.quality.features import evidence_status
from data.quality.freshness import candle_freshness_problems
from domain.models import INTERVAL_MS, Candle, Evidence, Snapshot


def kline_completeness(rows: list[Candle], expected: int) -> dict:
    gaps = find_gaps(rows)
    return {
        "count": len(rows),
        "closed_count": sum(r.closed for r in rows),
        "expected_minimum": expected,
        "gaps": gaps,
        "complete": len(rows) >= expected and all(r.closed for r in rows) and not gaps,
    }


def build_snapshot(
    info: dict,
    interval: str,
    end: int,
    records: list[Evidence],
    snapshot_minutes: int,
    *,
    warmup: int = 251,
    clock: dict | None = None,
    p2: bool = False,
    live: dict | None = None,
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
    completeness = {
        "analysis": kline_completeness(rows, warmup),
        "recent_1m": kline_completeness(recent_rows, snapshot_minutes),
    }
    if not completeness["analysis"]["complete"]:
        problems.append("analysis candles lack required closed, continuous warmup")
    problems.extend(recent_coverage_problems(recent_rows, snapshot_minutes))
    problems.extend(candle_freshness_problems(rows, recent_rows, end, INTERVAL_MS[interval]))
    required = required_evidence(records)
    problems.extend(required_evidence_problems(required))
    for record in required:
        state = evidence_status(record, end)
        if state != "VALID" and record.status == "ok":
            problems.append(f"required data {record.tool}: {state}")
    trading_valid = info.get("trading_rules", {}).get("symbol_trading_valid", False)
    if not trading_valid:
        problems.append("symbol trading rules are missing or invalid")
    sources = {
        r.tool: {
            "status": evidence_status(r, end),
            "observed_at": r.observed_at,
            "freshness_ms": end - r.observed_at if r.observed_at is not None else None,
            "evidence_id": r.id,
        }
        for r in records
    }
    snapshot = Snapshot(
        **info,
        interval=interval,
        as_of=end,
        candles=rows,
        recent_1m=recent_rows,
        indicators=indicators,
        order_book=book.summary,
        order_flow=flow.summary,
        derivatives={
            r.tool.removeprefix("get_"): r.summary
            for r in records[4:]
            if r.tool.removeprefix("get_") in METRICS
        },
        evidence_ids=[r.id for r in records],
        required_ids=[r.id for r in required],
        problems=problems,
        server_time_offset_ms=(clock or {}).get("server_time_offset_ms", 0),
        data_quality={
            "snapshot_as_of": end,
            "clock": clock or {},
            "kline_completeness": completeness,
            "symbol_trading_valid": trading_valid,
            "websocket_sequence_valid": live["websocket_sequence_valid"] if live else None,
            "data_freshness_ms": {name: row["freshness_ms"] for name, row in sources.items()},
            "sources": sources,
            "live_errors": live.get("errors", {}) if live else {},
        },
    )
    assemble_features(snapshot, records, p2=p2, live=live)
    for key, value in {
        "server_time_offset_ms": snapshot.server_time_offset_ms,
        "snapshot_as_of": end,
        "symbol_trading_valid": trading_valid,
        "websocket_sequence_valid": live["websocket_sequence_valid"] if live else None,
        "kline_completeness": completeness,
        "data_freshness_ms": snapshot.data_quality["data_freshness_ms"],
    }.items():
        snapshot.features[key] = value
        snapshot.feature_quality_status[key] = (
            "MISSING"
            if value is None
            else "INVALID"
            if key in {"symbol_trading_valid", "websocket_sequence_valid"} and not value
            else "INVALID"
            if key == "kline_completeness" and not all(v["complete"] for v in completeness.values())
            else "VALID"
        )
        snapshot.feature_metadata[key] = {
            "as_of": end,
            "unit": "ms" if key.endswith("ms") or key == "snapshot_as_of" else "status",
        }
    return snapshot
