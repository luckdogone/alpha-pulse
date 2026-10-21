import asyncio

import httpx
import pytest

from data.errors import SequenceError
from data.ingestion import transport
from data.ingestion.providers.binance import Binance
from data.ingestion.state import OrderBookState, TradeWindow
from data.ingestion.streaming import LiveMarket
from data.ingestion.transport import JsonHTTP
from domain.models import now_ms
from tests.data.test_stream import Socket


def snapshot(update_id=100):
    return {
        "lastUpdateId": update_id,
        "E": 1000,
        "T": 999,
        "bids": [["100", "2"], ["99", "3"]],
        "asks": [["101", "4"], ["102", "5"]],
    }


def depth(first=99, final=101, previous=98, at=1001, bids=None):
    return {
        "e": "depthUpdate",
        "s": "BTCUSDT",
        "E": at,
        "T": at - 1,
        "U": first,
        "u": final,
        "pu": previous,
        "b": bids or [],
        "a": [],
    }


def test_book_bridge_continuity_zero_deletes_and_preserves_reserve_levels():
    book = OrderBookState(levels=1)
    book.seed(snapshot())
    assert not book.valid
    assert not book.apply(depth(first=97, final=99))
    assert book.apply(depth(bids=[["100", "0"], ["98", "0"]]))
    current, _ = book.at(1001)
    assert current["bids"] == [[99.0, 3.0]]
    assert not book.apply(depth())  # Duplicate final update ID.
    assert book.apply(depth(first=102, final=103, previous=101, at=1002))
    current, previous = book.at(1002)
    assert previous["last_update_id"] == 101 and current["last_update_id"] == 103
    assert book.at(1001)[0]["last_update_id"] == 101
    with pytest.raises(SequenceError):
        book.apply(depth(first=104, final=105, previous=100, at=1003))
    assert not book.valid and book.at(1003) == (None, None)


def test_first_depth_event_must_overlap_snapshot():
    book = OrderBookState()
    book.seed(snapshot())
    with pytest.raises(SequenceError):
        book.apply(depth(first=101, final=102, previous=100))


def test_order_book_rebuilds_when_known_reserve_is_exhausted():
    book = OrderBookState(levels=1)
    book.seed(snapshot())
    book.apply(depth(bids=[["100", "0"]]))
    with pytest.raises(SequenceError, match="reserve depleted"):
        book.apply(depth(first=102, final=103, previous=101, bids=[["99", "0"], ["98", "10"]]))
    assert not book.valid


def test_trade_window_merges_rest_overlap_and_excludes_future_events():
    state = TradeWindow()
    rows = [{"a": i, "T": i * 1000, "p": "100", "q": "1", "m": False} for i in range(1, 4)]
    state.add(rows[-1])
    state.seed({"trades": rows[:2], "window_start": 0, "window_end": 3000, "complete": True})
    assert state.complete
    assert state.at(300_000, connected=True)["complete"]
    assert state.at(300_000, connected=True)["window_start"] == 0
    assert len(state.at(2500, connected=True)["trades"]) == 2
    assert state.add({**rows[-1], "a": 5, "T": 4000})
    assert not state.at(300_000, connected=True)["complete"]
    state.invalidate()
    assert not state.complete


@pytest.mark.parametrize(
    "route, expected, absent",
    [
        ("public", ["depth@500ms", "bookTicker"], ["aggTrade", "kline"]),
        (
            "market",
            ["kline_1m", "kline_15m", "aggTrade", "markPrice@1s", "forceOrder"],
            ["depth", "bookTicker"],
        ),
    ],
)
async def test_routed_combined_subscriptions(settings, monkeypatch, route, expected, absent):
    settings.enable_p2_features = True
    calls = []
    socket = Socket(
        [{"e": "bookTicker" if route == "public" else "aggTrade", "s": "BTCUSDT", "E": 1}]
    )

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return socket

    monkeypatch.setattr(transport, "connect", connect)
    stream = Binance(settings, None).market_stream("BTCUSDT", "15m", route)
    try:
        await anext(stream)
        url = calls[0][0]
        assert f"/{route}/stream?streams=" in url
        assert all(name in url for name in expected)
        assert all(name not in url for name in absent)
    finally:
        await stream.aclose()
    assert socket.closed


async def test_disconnect_during_depth_seed_cannot_publish_a_valid_book(settings):
    started, release = asyncio.Event(), asyncio.Event()
    provider = Binance(settings, None)
    at = now_ms()
    provider.clock.now = lambda: at

    async def fetch(*args, **kwargs):
        started.set()
        await release.wait()
        return snapshot()

    provider.depth = fetch
    live = LiveMarket(provider, "BTCUSDT", "15m")
    task = asyncio.create_task(live.apply("public", depth(at=at)))
    await started.wait()
    live.disconnected("public")
    release.set()
    await task
    assert live.view(at)["book"] is None
    assert not live.view(at)["websocket_sequence_valid"]


async def test_live_depth_gap_rebuild_and_stale_quote_rejection(settings):
    at = now_ms()
    ids = iter([100, 200])
    http = JsonHTTP(
        None, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot(next(ids))))
    )
    try:
        provider = Binance(settings, http)
        provider.clock.now = lambda: at
        live = LiveMarket(provider, "BTCUSDT", "15m")
        await live.apply("public", depth(at=at))
        assert live.view(at)["websocket_sequence_valid"]
        with pytest.raises(SequenceError):
            await live.apply("public", depth(first=102, final=103, previous=90, at=at))
        live._next_depth_sync = 0
        await live.apply("public", depth(first=199, final=201, previous=198, at=at))
        assert live.view(at)["book"]["last_update_id"] == 201
        await live.apply(
            "public",
            {
                "e": "bookTicker",
                "E": at - 6000,
                "b": "100",
                "a": "101",
                "B": "2",
                "A": "3",
                "u": 201,
            },
        )
        assert live.view(at)["ticker"] is None
    finally:
        await http.close()


async def test_force_order_uses_filled_quantity_and_deduplicates(settings):
    provider = Binance(settings, None)
    at = now_ms()
    provider.clock.now = lambda: at
    live = LiveMarket(provider, "BTCUSDT", "15m")
    event = {
        "e": "forceOrder",
        "E": at,
        "o": {
            "T": at,
            "S": "SELL",
            "p": "100",
            "q": "10",
            "ap": "99",
            "z": "2",
            "X": "PARTIALLY_FILLED",
        },
    }
    await live.apply("market", event)
    await live.apply("market", event)
    assert len(live.view(at)["liquidations"]) == 1
    assert live.view(at)["liquidations"][0]["quote"] == 198
    live.disconnected("market")
    assert live.view(at)["liquidation_coverage_start"] is None


async def test_context_start_cancellation_closes_producers(settings):
    provider = Binance(settings, None)
    live = LiveMarket(provider, "BTCUSDT", "15m")

    async def calibrate(*args, **kwargs):
        return {}

    async def backfill():
        await asyncio.sleep(0)
        raise asyncio.CancelledError()

    async def produce(route):
        await asyncio.Event().wait()

    provider.clock.calibrate = calibrate
    live.backfill = backfill
    live._produce = produce
    with pytest.raises(asyncio.CancelledError):
        await live.__aenter__()
    assert all(task.done() for task in live.tasks)


async def test_disconnect_during_trade_backfill_preserves_invalidity(settings):
    started, release = asyncio.Event(), asyncio.Event()
    provider = Binance(settings, None)
    live = LiveMarket(provider, "BTCUSDT", "15m")

    async def fetch(*args, **kwargs):
        started.set()
        await release.wait()
        return {"trades": [], "window_start": 0, "window_end": 300_000, "complete": True}

    provider.trades = fetch
    task = asyncio.create_task(live.backfill())
    await started.wait()
    live.disconnected("market")
    release.set()
    await task
    assert not live.trades.complete


async def test_fresh_ticker_cannot_hide_stale_depth(settings):
    provider = Binance(settings, None)
    at = now_ms()
    provider.clock.now = lambda: at
    live = LiveMarket(provider, "BTCUSDT", "15m")
    live.book.seed(snapshot())
    live.book.apply(depth(at=at - 6000))
    await live.apply(
        "public", {"e": "bookTicker", "E": at, "b": "100", "a": "101", "B": "1", "A": "1", "u": 101}
    )
    view = live.view(at)
    assert view["ticker"] is not None
    assert view["book"] is None and not view["websocket_sequence_valid"]


def test_queued_events_and_old_trades_cannot_claim_continuity(settings):
    live = LiveMarket(Binance(settings, None), "BTCUSDT", "15m")
    row = {"a": 100, "T": 300_000, "p": "100", "q": "1", "m": False}
    live.trades.seed({"trades": [row], "window_start": 0, "window_end": 300_000, "complete": True})
    live.received["market"] = 300_000
    assert not live.trades.add({**row, "a": 99, "T": -1})
    assert live.view(300_000)["trades"]["complete"]
    live.queue.put_nowait(("market", 0, {**row, "a": 101}))
    assert not live.view(300_000)["trades"]["complete"]
