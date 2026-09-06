import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from .agent import AgentError, AgentHarness, ClaudeModel, DeepSeekModel
from .binance import Binance
from .config import Settings
from .context import ContextWindow
from .decision import Decision, DecisionTree
from .demo import demo_snapshot
from .evidence import EvidenceStore
from .external import ExternalData
from .market import MarketData
from .models import INTERVAL_MS, Assessment, Prediction, Snapshot, normalize_symbol, now_ms
from .tools import ToolRegistry
from .transport import DataError, JsonHTTP


def prediction(
    *,
    run_id: str,
    symbol: str,
    interval: str,
    settings: Settings,
    store: EvidenceStore,
    engine: str,
    snapshot: Snapshot | None = None,
    decision: Decision | None = None,
    assessment: Assessment | None = None,
    zones: dict | None = None,
    reason_code: str = "insufficient_data",
    rationale: str = "数据不足，暂无合适时机。",
) -> Prediction:
    at = now_ms()
    trade = zones is not None
    if decision and reason_code == "insufficient_data" and not snapshot.problems:
        reason_code = decision.reason_code
    if assessment:
        rationale = assessment.rationale
    validity = settings.analysis_validity_minutes * 60
    evidence = []
    cited = set(assessment.evidence_ids if assessment and engine in {"claude", "deepseek"} else [])
    for row in store.ledger(at):
        evidence.append(
            {k: row[k] for k in ("id", "tool", "source", "status", "observed_at", "quality")}
            | {"cited_by_model": row["id"] in cited}
        )
    return Prediction(
        run_id=run_id,
        symbol=symbol,
        interval=interval,
        status="trade" if trade else "no_trade",
        direction=decision.direction if trade else "neutral",
        market_regime="trending"
        if trade
        else ("ranging" if reason_code in {"ranging", "low_volatility"} else "unknown"),
        reason_code="setup_confirmed" if trade else reason_code,
        generated_at=at,
        data_as_of=snapshot.as_of if snapshot else at,
        valid_from=at,
        valid_until=at + validity * 1000,
        validity_seconds=validity,
        confidence=assessment.confidence if assessment else 0,
        rationale=rationale,
        risks=(assessment.risks if assessment else []),
        evidence=evidence,
        decision_path=decision.path if decision else [],
        engine=engine,
        **(
            zones or {"reference_price": snapshot.order_book.get("mid_price") if snapshot else None}
        ),
    )


async def analyze(
    settings: Settings,
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    engine: str | None = None,
    scenario: str = "long",
) -> Prediction:
    symbol = normalize_symbol(symbol)
    engine = engine or settings.model_provider
    if engine not in {"claude", "deepseek", "rules", "demo"}:
        raise ValueError("engine must be claude, deepseek, rules, or demo")
    if interval not in INTERVAL_MS:
        raise ValueError("analysis interval must be a fixed Binance interval")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:10]
    directory = settings.artifacts_dir / run_id
    directory.mkdir(parents=True, exist_ok=False)
    store = EvidenceStore(directory / "evidence")
    tree = DecisionTree(settings.tree_file())
    http, external_http = (
        JsonHTTP(settings.proxy(binance=True), settings.request_timeout_seconds),
        JsonHTTP(settings.proxy(), settings.request_timeout_seconds),
    )
    binance = Binance(settings, http)
    market = MarketData(settings, binance, store)
    external = ExternalData(settings, external_http, store)
    model = None
    snapshot = None
    decision = None
    assessment = None
    common = dict(
        run_id=run_id,
        symbol=symbol,
        interval=interval,
        settings=settings,
        store=store,
        engine=engine,
    )
    try:
        async with asyncio.timeout(settings.analysis_timeout_seconds):
            snapshot = (
                demo_snapshot(store, scenario)
                if engine == "demo"
                else await market.snapshot(symbol, interval)
            )
            if engine == "demo":
                common.update(symbol=snapshot.symbol, interval=snapshot.interval)
            decision = tree.evaluate(snapshot, store, synthetic=engine == "demo")
            (directory / "snapshot.json").write_text(
                snapshot.model_dump_json(indent=2), encoding="utf-8"
            )
            if decision.reason_code == "insufficient_data":
                result = prediction(
                    **common,
                    snapshot=snapshot,
                    decision=decision,
                    rationale="必需行情缺失、过期或不完整，暂不生成方向和价格区间。",
                )
            else:
                if engine in {"claude", "deepseek"}:
                    model = (
                        DeepSeekModel(settings) if engine == "deepseek" else ClaudeModel(settings)
                    )
                    registry = ToolRegistry(market, external, store, settings.max_tool_calls)
                    context = ContextWindow(
                        snapshot,
                        decision,
                        store,
                        settings.context_max_bytes,
                        tree.config.model_dump(),
                    )
                    assessment = await AgentHarness(
                        model,
                        registry,
                        context,
                        settings.max_agent_rounds,
                        directory / "trace.jsonl",
                    ).run()
                else:
                    assessment = Assessment(
                        decision=decision.direction,
                        confidence=0.65 if decision.direction != "no_trade" else 0.5,
                        rationale="合成数据演示。"
                        if engine == "demo"
                        else "显式规则模式：由默认技术条件生成，未经过模型补充证据。",
                        evidence_ids=snapshot.required_ids,
                        risks=["默认决策树尚未经过回测；confidence 不是胜率。"],
                    )
                reason = decision.reason_code
                eligible = (
                    decision.direction != "no_trade" and assessment.decision == decision.direction
                )
                if decision.direction != "no_trade" and not eligible:
                    reason = "agent_veto"
                if eligible and assessment.confidence < tree.config.risk.minimum_confidence:
                    eligible, reason = False, "low_confidence"
                if eligible and engine in {"claude", "deepseek"}:
                    old_price, old_atr = (
                        snapshot.order_book["mid_price"],
                        snapshot.indicators["atr14"],
                    )
                    # LLM latency can outlive the book TTL: refresh every required market input.
                    fresh = await market.snapshot(symbol, interval)
                    fresh_decision = tree.evaluate(fresh, store)
                    drift = abs(fresh.order_book.get("mid_price", old_price) - old_price)
                    if (
                        fresh_decision.direction != decision.direction
                        or drift > old_atr * tree.config.risk.max_reference_drift_atr
                    ):
                        eligible, reason = False, "market_changed_during_analysis"
                    snapshot, decision = fresh, fresh_decision
                    (directory / "final_snapshot.json").write_text(
                        snapshot.model_dump_json(indent=2), encoding="utf-8"
                    )
                if eligible:
                    final_decision = tree.evaluate(snapshot, store, synthetic=engine == "demo")
                    if final_decision.direction != decision.direction:
                        eligible, reason = False, "stale_data_before_output"
                    decision = final_decision
                zones = tree.zones(snapshot, decision.direction) if eligible else None
                if not eligible and reason in {
                    "market_changed_during_analysis",
                    "stale_data_before_output",
                }:
                    assessment = assessment.model_copy(
                        update={
                            "decision": "no_trade",
                            "confidence": 0,
                            "rationale": "分析期间行情已变化或过期，需重新分析。",
                        }
                    )
                result = prediction(
                    **common,
                    snapshot=snapshot,
                    decision=decision,
                    assessment=assessment,
                    zones=zones,
                    reason_code=reason,
                )
    except (DataError, AgentError, ValueError, KeyError, TypeError, TimeoutError) as exc:
        code = (
            "analysis_timeout"
            if isinstance(exc, TimeoutError)
            else (
                "model_unavailable" if isinstance(exc, AgentError) else "data_or_validation_error"
            )
        )
        message = str(exc) if isinstance(exc, (DataError, AgentError)) else type(exc).__name__
        result = prediction(
            **(common | {"engine": "guard"}),
            snapshot=snapshot,
            decision=decision,
            reason_code=code,
            rationale=f"未生成预测：{message}。",
        )
    finally:
        if model:
            await model.close()
        await http.close()
        await external_http.close()
    (directory / "prediction.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (directory / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "tree": tree.config.model_dump(),
                "engine": engine,
                "model": settings.model_name(engine) if engine in {"claude", "deepseek"} else None,
                "model_fallbacks": settings.anthropic_fallbacks if engine == "claude" else False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return result
