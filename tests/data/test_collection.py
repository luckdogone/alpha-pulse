import json

import httpx
import pytest
from pydantic import SecretStr

from agent.context import ContextWindow
from agent.contracts import ASSESSMENT_SCHEMA, dumps
from agent.tools import ToolRegistry
from data.errors import DataError, DataUnavailable
from data.ingestion import clock as clock_module
from data.ingestion.clock import ServerClock
from data.ingestion.providers.binance import Binance
from data.ingestion.streaming import LiveMarket
from data.ingestion.transport import JsonHTTP
from data.pipeline import ExternalData, MarketData
from domain.decision import DecisionTree
from domain.models import now_ms
from storage.evidence import EvidenceStore
from tests.data.fixtures import binance_response


async def test_clock_uses_lowest_rtt_midpoint_and_caches(monkeypatch):
    ticks = iter([1000, 1020, 2000, 2004, 3000, 3100, 4000])
    servers = iter([1110, 2102, 3180])
    monkeypatch.setattr(clock_module, "now_ms", lambda: next(ticks))
    calls = []

    async def get(path):
        calls.append(path)
        return {"serverTime": next(servers)}

    clock = ServerClock(300, 3)
    report = await clock.calibrate(get)
    assert report == {"server_time_offset_ms": 100, "round_trip_ms": 4, "calibrated_at": 4100}
    assert await clock.calibrate(get) == report
    assert len(calls) == 3


async def test_aggregate_pagination_uses_ids_without_losing_same_millisecond_trades(settings):
    end = now_ms()
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        if "fromId" not in params:
            assert int(params["limit"]) == 1000
            assert int(params["endTime"]) - int(params["startTime"]) == 300_000
            rows = [{"a": i, "T": end - 1, "p": "100", "q": "1", "m": False} for i in range(1000)]
        else:
            assert params["fromId"] == "1000"
            assert "startTime" not in params and "endTime" not in params
            rows = [
                {"a": 1000, "T": end - 1, "p": "100", "q": "1", "m": True},
                {"a": 1001, "T": end + 1, "p": "100", "q": "1", "m": True},
            ]
        return httpx.Response(200, json=rows)

    http = JsonHTTP(None, transport=httpx.MockTransport(handler))
    try:
        data = await Binance(settings, http).trades("BTCUSDT", end_ms=end)
        assert len(calls) == 2 and len(data["trades"]) == 1001
        assert data["complete"] and not data["truncated"]
        assert max(r["T"] for r in data["trades"]) < end
    finally:
        await http.close()


async def test_clock_skew_full_feature_context_fits_budget_and_masks_bad_sources(settings):
    settings.enable_p2_features = True
    settings.binance_api_key = SecretStr("test-key")
    at = now_ms() + 120_000
    http = JsonHTTP(
        None,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=binance_response(r, at))),
    )
    try:
        provider, store = Binance(settings, http), EvidenceStore()
        market = MarketData(settings, provider, store)
        snapshot = await market.snapshot("BTCUSDT", "15m")
        assert snapshot.server_time_offset_ms > 119_000
        assert not snapshot.problems
        assert (
            snapshot.data_quality["sources"]["get_recent_1m"]["observed_at"]
            == snapshot.recent_1m[-1].close_time
        )
        assert all(
            row["quality"]["level"] == "high"
            for row in store.ledger()
            if row["tool"] == "get_order_book"
        )
        tree = DecisionTree(settings.tree_file())
        assert snapshot.feature_quality_status["orderbook_imbalance"] == "MISSING"
        assert snapshot.features["orderbook_imbalance"] is None
        assert snapshot.feature_quality_status["estimated_slippage_bps"] == "VALID"
        assert tree.evaluate(snapshot, store.records).reason_code == "insufficient_data"
        registry = ToolRegistry(market, ExternalData(settings, http, store), store, 16)
        context = ContextWindow(
            snapshot,
            tree.evaluate(snapshot, store.records),
            store,
            settings.context_max_bytes,
            tree.config.model_dump(),
        )
        context.reserved_bytes = (
            len(dumps(registry.definitions()).encode())
            + len(dumps(ASSESSMENT_SCHEMA).encode())
            + 512
        )
        assert context.messages()
        snapshot.data_quality["sources"]["get_order_book"]["status"] = "STALE"
        snapshot.feature_quality_status["mid_price"] = "STALE"
        snapshot.data_quality["sources"]["get_klines"]["status"] = "INVALID"
        pinned = json.loads(context.messages()[0]["content"])["market_snapshot"]
        assert pinned["order_book"] == {"quality_status": "STALE"}
        assert pinned["indicators"] == {"quality_status": "INVALID"}
        assert "mid_price" not in pinned["features"]
    finally:
        await http.close()


async def test_live_snapshot_uses_stream_sources_with_evidence_provenance(settings):
    at = now_ms()

    def handler(request):
        row = binance_response(request, at)
        if request.url.path.endswith("premiumIndex"):
            row["time"] = at - 1000
        return httpx.Response(200, json=row)

    http = JsonHTTP(None, transport=httpx.MockTransport(handler))
    try:
        provider, store = Binance(settings, http), EvidenceStore()
        provider.clock.now = lambda: at
        live = LiveMarket(provider, "BTCUSDT", "15m")
        await live.backfill()
        await live.apply(
            "public", {"e": "depthUpdate", "E": at, "U": 99, "u": 101, "pu": 98, "b": [], "a": []}
        )
        await live.apply(
            "public",
            {"e": "bookTicker", "E": at, "u": 101, "b": "100", "a": "100.2", "B": "2", "A": "3"},
        )
        await live.apply(
            "market",
            {
                "e": "markPriceUpdate",
                "E": at,
                "T": at + 3_600_000,
                "p": "101",
                "i": "100",
                "r": "0.0002",
            },
        )
        snapshot = await MarketData(settings, provider, store, live).snapshot("BTCUSDT", "15m")
        assert snapshot.order_book["source"] == "binance_ws_depth"
        assert snapshot.order_flow["source"] == "binance_ws_with_rest_backfill"
        assert snapshot.features["best_ask"] == 100.2
        assert snapshot.features["funding_rate_current"] == 0.0002
        assert snapshot.features["websocket_sequence_valid"] is True
        assert snapshot.feature_quality_status["orderbook_imbalance"] == "MISSING"
        assert snapshot.features["orderbook_imbalance"] is None
        assert snapshot.feature_quality_status["estimated_slippage_bps"] == "VALID"
        tree = DecisionTree(settings.tree_file())
        assert tree.evaluate(snapshot, store.records).reason_code == "insufficient_data"
        source_id = snapshot.feature_metadata["funding_rate_current"]["evidence_ids"][0]
        assert store.records[source_id].tool == "get_mark_price_stream"
    finally:
        await http.close()


async def test_trade_page_budget_marks_partial_and_withholds_cvd(settings):
    settings.trade_max_pages = 1
    end = now_ms()
    rows = [{"a": i, "T": end - 1, "p": "100", "q": "1", "m": False} for i in range(1000)]
    http = JsonHTTP(None, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rows)))
    try:
        record = await MarketData(settings, Binance(settings, http), EvidenceStore()).order_flow(
            "BTCUSDT"
        )
        assert record.status == "partial" and record.summary["truncated"]
        assert record.summary["next_from_id"] == 1000
        assert record.summary["cvd_1m"] is record.summary["cvd_5m"] is None
    finally:
        await http.close()


async def test_nonadvancing_trade_pagination_fails(settings):
    end = now_ms()
    rows = [{"a": i, "T": end - 1} for i in range(1000)]
    http = JsonHTTP(None, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rows)))
    try:
        with pytest.raises(DataError, match="did not advance"):
            await Binance(settings, http).trades("BTCUSDT", end_ms=end)
    finally:
        await http.close()


async def test_top_trader_requires_key_and_only_sends_header_to_protected_endpoints(settings):
    calls, at = [], now_ms()

    def handler(request):
        calls.append(request)
        if "topLongShort" not in request.url.path:
            assert "X-MBX-APIKEY" not in request.headers
        return httpx.Response(200, json=binance_response(request, at))

    http = JsonHTTP(None, transport=httpx.MockTransport(handler))
    try:
        provider = Binance(settings, http)
        with pytest.raises(DataUnavailable, match="BINANCE_API_KEY"):
            await provider.derivative("BTCUSDT", "top_account_long_short_ratio")
        assert calls == []
        settings.binance_api_key = SecretStr("test-key")
        for name in (
            "top_account_long_short_ratio",
            "top_position_long_short_ratio",
            "global_long_short_ratio",
        ):
            await provider.derivative("BTCUSDT", name)
        assert all(r.url.params["limit"] == "50" for r in calls)
        assert all(r.url.params["period"] == "5m" for r in calls)
    finally:
        await http.close()


async def test_snapshot_windows_rules_funding_and_caches(settings):
    settings.enable_p2_features = True
    settings.binance_api_key = SecretStr("test-key")
    requests, at = [], now_ms()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=binance_response(request, at))

    http = JsonHTTP(None, transport=httpx.MockTransport(handler))
    try:
        provider = Binance(settings, http)
        store = EvidenceStore()
        market = MarketData(settings, provider, store)
        snapshot = await market.snapshot("BTCUSDT", "15m")
        assert not snapshot.problems
        klines = {
            r.url.params["interval"]: int(r.url.params["limit"])
            for r in requests
            if r.url.path.endswith("klines")
        }
        assert klines == {"15m": 253, "1m": 35, "1h": 122, "4h": 122}
        oi = [dict(r.url.params) for r in requests if r.url.path.endswith("openInterestHist")]
        assert {(r["period"], r["limit"]) for r in oi} == {("5m", "50"), ("1h", "26")}
        assert all(c.closed for c in snapshot.candles + snapshot.recent_1m)
        assert len(snapshot.candles) == 251 and len(snapshot.recent_1m) == 31
        assert snapshot.derivatives["funding_info"]["adjustment_present"] is False
        funding = next(r for r in store.records.values() if r.tool == "get_funding_info")
        assert funding.status == "ok"
        assert snapshot.feature_quality_status["htf_trend_alignment"] == "VALID"
        assert snapshot.feature_quality_status["crowding_risk_score"] == "VALID"
        assert snapshot.feature_quality_status["liquidation_buy_usdt"] == "MISSING"
        assert snapshot.features["oi_change_4h"] == pytest.approx(48 / 1001)
        assert snapshot.features["taker_buy_ratio_30m"] == 0.6
        assert snapshot.features["taker_delta_30m"] == 6000
        assert snapshot.features["funding_rate_mean_30"] == pytest.approx(0.0001145)
        assert snapshot.features["funding_rate_trend"] == pytest.approx(0.000001)
        assert snapshot.features["mark_index_basis_pct"] == pytest.approx((100 / 99.9 - 1) * 100)
        count = len(requests)
        second = await market.snapshot("BTCUSDT", "15m")
        repeated = [r.url.path for r in requests[count:]]
        assert "/fapi/v1/exchangeInfo" not in repeated and "/fapi/v1/time" not in repeated
        assert "/fapi/v1/fundingInfo" not in repeated and "/fapi/v1/klines" not in repeated
        assert "/fapi/v1/premiumIndex" in repeated
        assert set(second.evidence_ids).isdisjoint(snapshot.evidence_ids)
    finally:
        await http.close()


async def test_missing_key_degrades_only_dependent_features(settings):
    at = now_ms()
    http = JsonHTTP(
        None,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=binance_response(r, at))),
    )
    try:
        snapshot = await MarketData(settings, Binance(settings, http), EvidenceStore()).snapshot(
            "BTCUSDT", "1h"
        )
        assert not snapshot.problems
        assert snapshot.feature_quality_status["top_account_ls_ratio"] == "MISSING"
        assert snapshot.features["top_account_ls_ratio"] is None
        assert snapshot.feature_quality_status["oi_change_24h"] == "VALID"
    finally:
        await http.close()


@pytest.mark.parametrize("bad_filter", [None, "0", "NaN"])
async def test_invalid_trading_price_rule_is_rejected(settings, bad_filter):
    def handler(request):
        data = binance_response(request, now_ms())
        filters = data["symbols"][0]["filters"]
        if bad_filter is None:
            filters.pop(0)
        else:
            filters[0]["tickSize"] = bad_filter
        return httpx.Response(200, json=data)

    http = JsonHTTP(None, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(DataError):
            await Binance(settings, http).symbol_info("BTCUSDT")
    finally:
        await http.close()
