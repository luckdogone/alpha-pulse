"""Complete, deterministic Binance response fixtures; no live account or network access."""

from domain.models import INTERVAL_MS


def rest_candle(start, duration=60_000):
    return [
        start,
        "100",
        "102",
        "99",
        "101",
        "1000",
        start + duration - 1,
        "100500",
        100,
        "600",
        "60300",
        "0",
    ]


def binance_response(request, at):
    path, params = request.url.path, request.url.params
    if path.endswith("exchangeInfo"):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.1",
                            "minPrice": "0.1",
                            "maxPrice": "1000000",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                            "maxQty": "1000",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }
    if path.endswith("/time"):
        return {"serverTime": at}
    if path.endswith("/klines"):
        duration = INTERVAL_MS[params["interval"]]
        count = int(params["limit"])
        end = int(params.get("endTime", at))
        start = end // duration * duration - (count - 1) * duration
        return [rest_candle(start + i * duration, duration) for i in range(count)]
    if path.endswith("/depth"):
        return {
            "E": at,
            "T": at,
            "lastUpdateId": 100,
            "bids": [["100", "1000"], ["99.9", "1000"]],
            "asks": [["100.1", "1000"], ["100.2", "1000"]],
        }
    if path.endswith("/aggTrades"):
        start, end = int(params.get("startTime", at - 300_000)), int(params.get("endTime", at))
        return [
            {
                "a": i,
                "p": "100",
                "q": "1",
                "T": start + (end - start) * i // 10,
                "m": i % 3 == 0,
                "f": i,
                "l": i,
            }
            for i in range(10)
        ]
    if path.endswith("/premiumIndex"):
        return {
            "symbol": "BTCUSDT",
            "time": at,
            "lastFundingRate": ".0001",
            "markPrice": "100",
            "indexPrice": "99.9",
            "nextFundingTime": at + 3_600_000,
        }
    if path.endswith("/openInterest"):
        return {"symbol": "BTCUSDT", "time": at, "openInterest": "1000"}
    if path.endswith("/fundingRate"):
        end = int(params.get("endTime", at)) // 28_800_000 * 28_800_000
        return [
            {
                "symbol": "BTCUSDT",
                "fundingTime": end - (29 - i) * 28_800_000,
                "fundingRate": str(0.0001 + 0.000001 * i),
                "markPrice": "100",
            }
            for i in range(30)
        ]
    if path.endswith("/fundingInfo"):
        return []
    if path.startswith("/futures/data/"):
        period = INTERVAL_MS[params["period"]]
        end = int(params.get("endTime", at)) // period * period
        count = int(params["limit"])
        if "topLongShort" in path:
            assert request.headers["X-MBX-APIKEY"] == "test-key"
        return [
            {
                "symbol": "BTCUSDT",
                "timestamp": end - (count - 1 - i) * period,
                "sumOpenInterest": str(1000 + i),
                "sumOpenInterestValue": str((1000 + i) * 100),
                "longShortRatio": str(1 + i * 0.001),
                "longAccount": "0.6",
                "shortAccount": "0.4",
                "buyVol": "60",
                "sellVol": "40",
                "buySellRatio": "1.5",
            }
            for i in range(count)
        ]
    raise AssertionError(f"unexpected request: {path}")
