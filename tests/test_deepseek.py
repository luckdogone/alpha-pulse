import json

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from pydantic import SecretStr, ValidationError

from alpha_pulse.agent import AgentError, DeepSeekModel
from alpha_pulse.config import Settings
from alpha_pulse.models import Assessment


def test_deepseek_requires_its_own_key(settings):
    settings.deepseek_api_key = None
    settings.anthropic_api_key = SecretStr("unrelated-claude-test-key")
    with pytest.raises(AgentError, match="DEEPSEEK_API_KEY"):
        DeepSeekModel(settings)


def test_deepseek_rejects_unknown_model_names():
    # The compatible endpoint silently maps unknown names; prevent accidental aliases.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, deepseek_model="deepseek-typo")


async def test_deepseek_sdk_request_stream_and_thinking_replay(settings, monkeypatch):
    settings.deepseek_api_key = SecretStr("test-deepseek-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unrelated-claude-test-token")
    assessment = {
        "decision": "no_trade",
        "confidence": 0.5,
        "rationale": "证据不足，暂不交易。",
        "evidence_ids": ["ev_1234567890abcdef"],
        "risks": [],
    }
    thinking = {"type": "thinking", "thinking": "test reasoning", "signature": "test-signature"}
    messages = [
        {"role": "user", "content": "Analyze the supplied fixture."},
        {
            "role": "assistant",
            "content": [
                thinking,
                {"type": "tool_use", "id": "t1", "name": "save_note", "input": {"note": "test"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "saved"}],
        },
    ]
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == "https://api.deepseek.com/anthropic/v1/messages"
        assert request.headers["x-api-key"] == "test-deepseek-key"
        assert "authorization" not in request.headers
        assert body["model"] == "deepseek-v4-pro"
        assert body["stream"] is True
        assert body["thinking"]["type"] == "enabled"
        assert body["output_config"] == {"effort": "high"}
        assert "fallbacks" not in body
        assert "JSON Schema" in body["system"]
        assert body["tool_choice"] == {"type": "none"}
        assert body["messages"][1]["content"][0] == thinking
        start = {
            "id": "msg_deepseek_test",
            "type": "message",
            "role": "assistant",
            "model": "deepseek-v4-pro",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 0},
        }
        events = [
            {"type": "message_start", "message": start},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": json.dumps(assessment)},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 30},
            },
            {"type": "message_stop"},
        ]
        stream = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    original_client = anthropic.DefaultAsyncHttpxClient
    monkeypatch.setattr(
        anthropic,
        "DefaultAsyncHttpxClient",
        lambda **kwargs: original_client(transport=httpx2.MockTransport(handler), **kwargs),
    )
    model = DeepSeekModel(settings)
    try:
        result = await model.complete(messages, [], final=True)
        assert Assessment.model_validate_json(result.content[0].text).decision == "no_trade"
        assert len(requests) == 1
    finally:
        await model.close()


async def test_service_uses_configured_deepseek_provider(settings, monkeypatch):
    from alpha_pulse import service
    from alpha_pulse.demo import demo_snapshot
    from alpha_pulse.market import MarketData

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
