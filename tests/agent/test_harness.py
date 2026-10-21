import json

import anthropic
import httpx2
import pytest

from agent.context import ContextWindow
from agent.contracts import AgentError, dumps
from agent.harness import AgentHarness
from agent.prompts import SYSTEM_PROMPT
from agent.providers.claude import ClaudeModel
from agent.tools import ToolRegistry
from data.pipeline import ExternalData, MarketData
from domain.models import Assessment
from tests.helpers import response


class ScriptedModel:
    def __init__(self, messages):
        self.responses = iter(messages)
        self.requests = []

    async def complete(self, messages, tools, final):
        self.requests.append({"messages": messages, "tools": tools, "final": final})
        return next(self.responses)


@pytest.fixture
def registry(settings, market_fixture):
    _, store, _ = market_fixture
    # These tests use only local tools; reaching the network is an error.
    return ToolRegistry(
        MarketData(settings, None, store), ExternalData(settings, None, store), store, 16
    )


def assessment_response(snapshot, **overrides):
    value = {
        "decision": "long",
        "confidence": 0.7,
        "rationale": "趋势与买盘一致。",
        "evidence_ids": [snapshot.required_ids[0]],
        "risks": ["未经回测"],
    } | overrides
    return response([{"type": "text", "text": json.dumps(value)}])


async def test_agent_selects_tools_returns_pairs_and_preserves_signed_blocks(
    market_fixture, registry
):
    snapshot, store, tree = market_fixture
    evidence_id = snapshot.required_ids[0]
    calls = [
        {"type": "thinking", "thinking": "", "signature": "test-signed-block"},
        {
            "type": "tool_use",
            "id": "t1",
            "name": "read_evidence",
            "input": {"evidence_id": evidence_id, "limit": 2},
        },
        {
            "type": "tool_use",
            "id": "t2",
            "name": "save_note",
            "input": {"note": f"已检查 {evidence_id}；继续评估风险。"},
        },
    ]
    model = ScriptedModel([response(calls, "tool_use"), assessment_response(snapshot)])
    context = ContextWindow(
        snapshot, tree.evaluate(snapshot, store.records), store, 48000, tree.config.model_dump()
    )
    harness = AgentHarness(model, registry, context, 3)
    result = await harness.run()
    assert result.decision == "long"
    assert registry.calls == 2
    second = model.requests[1]["messages"]
    assert second[-2]["content"][0]["signature"] == "test-signed-block"
    assert [r["tool_use_id"] for r in second[-1]["content"]] == ["t1", "t2"]
    assert "test-signed-block" not in dumps(harness.trace)
    assert registry.note in second[0]["content"]


async def test_agent_rejects_invented_evidence_and_repairs(market_fixture, registry):
    snapshot, store, tree = market_fixture
    model = ScriptedModel(
        [assessment_response(snapshot, evidence_ids=["invented"]), assessment_response(snapshot)]
    )
    context = ContextWindow(
        snapshot, tree.evaluate(snapshot, store.records), store, 48000, tree.config.model_dump()
    )
    result = await AgentHarness(model, registry, context, 3).run()
    assert result.evidence_ids == [snapshot.required_ids[0]]
    assert "validation feedback" in model.requests[1]["messages"][0]["content"]


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "pause_turn"])
async def test_agent_never_accepts_incomplete_output(market_fixture, registry, stop):
    snapshot, store, tree = market_fixture
    model = ScriptedModel([response([], stop)])
    context = ContextWindow(
        snapshot, tree.evaluate(snapshot, store.records), store, 48000, tree.config.model_dump()
    )
    with pytest.raises(AgentError):
        await AgentHarness(model, registry, context, 2).run()


async def test_tool_budget_and_invalid_inputs_are_recoverable(registry):
    result, failed = await registry.call("get_klines", {"symbol": "BTCUSDT", "interval": "oops"})
    assert failed and result["status"] == "error"
    registry.max_calls = registry.calls
    result, failed = await registry.call("save_note", {"note": "too late"})
    assert failed and "budget" in result["error"]
    assert not registry.note


async def test_read_evidence_pages_nested_raw_data(market_fixture, registry):
    snapshot, store, _ = market_fixture
    record = store.add(
        tool="get_order_flow",
        source="binance",
        symbol=snapshot.symbol,
        observed_at=snapshot.as_of,
        ttl_ms=60_000,
        summary={"count": 500},
        data={"trades": [{"id": i, "price": 100} for i in range(500)]},
    )
    result, failed = await registry.call(
        "read_evidence", {"evidence_id": record.id, "field": "trades", "offset": 10, "limit": 2}
    )
    assert not failed
    assert result["data"] == [{"id": 10, "price": 100}, {"id": 11, "price": 100}]
    assert result["next_offset"] == 12


def test_context_compacts_complete_exchanges_retaining_snapshot(market_fixture):
    snapshot, store, tree = market_fixture
    context = ContextWindow(
        snapshot, tree.evaluate(snapshot, store.records), store, 26000, tree.config.model_dump()
    )
    for i in range(4):
        context.add_exchange(
            [{"type": "tool_use", "id": f"t{i}", "name": "read_evidence", "input": {}}],
            [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * 8000}],
        )
    messages = context.messages("保留矛盾证据")
    pinned = json.loads(messages[0]["content"])
    assert context.archived_exchanges > 0
    assert pinned["market_snapshot"]["recent_1m"]
    assert len(pinned["market_snapshot"]["recent_1m"]) == 31
    assert pinned["working_note_untrusted"] == "保留矛盾证据"
    assert len(dumps(messages).encode()) + len(SYSTEM_PROMPT.encode()) <= context.max_bytes
    for offset in range(1, len(messages), 2):
        assert (
            messages[offset]["content"][0]["id"]
            == messages[offset + 1]["content"][0]["tool_use_id"]
        )
    assert len(store.records) == 6


async def test_official_sdk_stream_request_and_final_json(settings, market_fixture):
    """Exercise actual SDK serialization + SSE parsing through a mock HTTP transport."""
    snapshot = market_fixture[0]
    text = assessment_response(snapshot).content[0].text
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["stream"] is True
        assert body["fallbacks"] == "default"
        assert body["thinking"]["type"] == "adaptive"
        assert body["output_config"]["format"]["type"] == "json_schema"
        events = [
            {"type": "message_start", "message": response([]).model_dump()},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
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

    model = ClaudeModel.__new__(ClaudeModel)
    model.settings = settings
    model.client = anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(handler), trust_env=False
        ),
    )
    try:
        result = await model.complete(
            [{"role": "user", "content": "Analyze fixture"}], [], final=True
        )
        assert Assessment.model_validate_json(result.content[0].text).decision == "long"
        assert len(requests) == 1
    finally:
        await model.close()
