import json

import pytest

from alpha_pulse import binance as module
from alpha_pulse.binance import Binance
from alpha_pulse.transport import DataError


def event(timestamp, opened=60_000, closed=False, symbol="BTCUSDT"):
    return {
        "e": "kline",
        "E": timestamp,
        "s": symbol,
        "k": {
            "t": opened,
            "T": opened + 59_999,
            "s": symbol,
            "i": "1m",
            "o": "100",
            "h": "102",
            "l": "99",
            "c": "101",
            "v": "100",
            "q": "10050",
            "n": 10,
            "V": "60",
            "x": closed,
        },
    }


class Socket:
    def __init__(self, messages):
        self.messages = iter(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def recv(self):
        return json.dumps(next(self.messages))


async def test_stream_uses_proxy_filters_wrong_symbol_and_duplicate_closes(settings, monkeypatch):
    settings.binance_proxy = "http://127.0.0.1:13659"
    calls = []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return Socket(
            [
                event(100, symbol="ETHUSDT"),
                event(101, closed=True),
                event(102, closed=True),
                event(100, opened=120_000),
                {"stream": "btcusdt@kline_1m", "data": event(103, opened=120_000)},
            ]
        )

    monkeypatch.setattr(module, "connect", connect)
    stream = Binance(settings, None).stream("btcusdt", "1m")
    try:
        first, second = await anext(stream), await anext(stream)
        assert first["candle"]["closed"]
        assert second["event_time"] == 103
        assert calls[0][0] == "wss://fstream.binance.com/market/ws/btcusdt@kline_1m"
        assert calls[0][1]["proxy"] == "http://127.0.0.1:13659"
    finally:
        await stream.aclose()


async def test_reconnect_attempts_are_bounded(settings, monkeypatch):
    calls, delays = [], []

    def connect(url, **kwargs):
        calls.append(url)
        raise OSError("simulated proxy failure")

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module, "connect", connect)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    stream = Binance(settings, None).stream("BTCUSDT", "1m")
    with pytest.raises(DataError, match="6 reconnect attempts"):
        await anext(stream)
    assert len(calls) == 6
    assert delays == [1, 2, 4, 8, 10]
