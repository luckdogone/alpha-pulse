import json

from anthropic.types import Message

from data.market import MarketData
from tests.helpers import response


async def test_service_fails_closed_on_provider_error(settings, monkeypatch):
    from application.service import analyze
    from data.transport import DataError

    async def unavailable(*args, **kwargs):
        raise DataError("HTTP 451")

    monkeypatch.setattr(MarketData, "snapshot", unavailable)
    result = await analyze(settings, engine="rules")
    assert result.status == "no_trade" and result.market_regime == "unknown"
    assert result.engine == "guard"
    assert result.entry_zone is None


async def test_service_rechecks_price_after_model(settings, market_fixture, monkeypatch):
    from application import service
    from application.demo import demo_snapshot

    count = 0

    async def snapshot(market, symbol, interval):
        nonlocal count
        count += 1
        result = demo_snapshot(market.store)
        for record in market.store.records.values():
            record.source = "binance"
        if count > 1:
            result.order_book["mid_price"] += result.indicators["atr14"] * 4
        return result

    class Model:
        def __init__(self, settings):
            pass

        async def complete(self, messages, tools, final):
            pinned = json.loads(messages[0]["content"])
            evidence_id = pinned["market_snapshot"]["required_evidence_ids"][0]
            return response(
                [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "decision": "long",
                                "confidence": 0.8,
                                "rationale": "Fixture",
                                "evidence_ids": [evidence_id],
                                "risks": [],
                            }
                        ),
                    }
                ]
            )

        async def close(self):
            pass

    monkeypatch.setattr(service.MarketData, "snapshot", snapshot)
    monkeypatch.setattr(service, "ClaudeModel", Model)
    result = await service.analyze(settings)
    assert count == 2
    assert result.reason_code == "market_changed_during_analysis"
    assert result.status == "no_trade" and result.entry_zone is None


async def test_service_uses_configured_deepseek_provider(settings, monkeypatch):
    from application import service
    from application.demo import demo_snapshot
    from data.market import MarketData

    settings.model_provider = "deepseek"

    async def snapshot(market, symbol, interval):
        result = demo_snapshot(market.store)
        for record in market.store.records.values():
            record.source = "binance"
        return result

    class Model:
        def __init__(self, configuration):
            assert configuration.model_provider == "deepseek"

        async def complete(self, messages, tools, final):
            pinned = json.loads(messages[0]["content"])
            assessment = {
                "decision": "no_trade",
                "confidence": 0.5,
                "rationale": "等待更清晰信号。",
                "evidence_ids": [pinned["market_snapshot"]["required_evidence_ids"][0]],
                "risks": [],
            }
            return Message(
                id="msg_deepseek_test",
                type="message",
                role="assistant",
                model="deepseek-v4-pro",
                content=[{"type": "text", "text": json.dumps(assessment)}],
                stop_reason="end_turn",
                stop_sequence=None,
                usage={"input_tokens": 100, "output_tokens": 30},
            )

        async def close(self):
            pass

    monkeypatch.setattr(MarketData, "snapshot", snapshot)
    monkeypatch.setattr(service, "DeepSeekModel", Model)
    result = await service.analyze(settings)
    assert result.engine == "deepseek"
    assert result.status == "no_trade"
    assert any(record["cited_by_model"] for record in result.evidence)
    metadata = json.loads((settings.artifacts_dir / result.run_id / "run.json").read_text())
    assert metadata["model"] == "deepseek-v4-pro"
    assert metadata["model_fallbacks"] is False
