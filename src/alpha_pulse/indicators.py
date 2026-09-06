from math import sqrt
from statistics import mean

from .models import Candle


def ema(values: list[float], period: int) -> list[float]:
    """SMA-seeded EMA. Output starts at input index period - 1."""
    if len(values) < period:
        return []
    out = [mean(values[:period])]
    alpha = 2 / (period + 1)
    for value in values[period:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def wilder(values: list[float], period: int) -> float:
    value = mean(values[:period])
    for current in values[period:]:
        value = (value * (period - 1) + current) / period
    return value


def calculate(candles: list[Candle]) -> dict:
    rows = [c for c in candles if c.closed]
    if len(rows) < 60:
        raise ValueError("at least 60 closed candles are required for indicator warmup")
    closes = [r.close for r in rows]
    differences = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    gain = wilder([max(d, 0) for d in differences], 14)
    loss = wilder([max(-d, 0) for d in differences], 14)
    rsi = 50.0 if gain == loss == 0 else (100.0 if loss == 0 else 100 - 100 / (1 + gain / loss))
    fast, slow = ema(closes, 12), ema(closes, 26)
    macd_line = [f - s for f, s in zip(fast[14:], slow, strict=True)]
    signal = ema(macd_line, 9)[-1]
    tr = [
        max(r.high - r.low, abs(r.high - p.close), abs(r.low - p.close))
        for p, r in zip(rows, rows[1:], strict=False)
    ]
    atr = wilder(tr, 14)
    middle = mean(closes[-20:])
    std = sqrt(mean((v - middle) ** 2 for v in closes[-20:]))
    ema9, ema21, ema50 = (ema(closes, n)[-1] for n in (9, 21, 50))
    volume = sum(r.volume for r in rows[-30:])
    # Rolling VWAP from exchange quote/base volume, not a session-anchored VWAP.
    vwap = sum(r.quote_volume for r in rows[-30:]) / volume if volume else None
    avg_volume = mean(r.volume for r in rows[-21:-1])
    return {
        "source": "locally_computed_from_binance_closed_klines",
        "closed_candles": len(rows),
        "as_of": rows[-1].close_time,
        "last_close": closes[-1],
        "ema9": ema9,
        "ema21": ema21,
        "ema50": ema50,
        "ema_alignment": 1 if ema9 > ema21 > ema50 else (-1 if ema9 < ema21 < ema50 else 0),
        "rsi14": rsi,
        "macd": macd_line[-1],
        "macd_signal": signal,
        "macd_histogram": macd_line[-1] - signal,
        "atr14": atr,
        "atr_pct": atr / closes[-1] * 100,
        "trend_strength_atr": abs(ema9 - ema21) / atr if atr else 0,
        "bollinger_middle": middle,
        "bollinger_upper": middle + 2 * std,
        "bollinger_lower": middle - 2 * std,
        "bollinger_width_pct": 4 * std / middle * 100,
        "rolling_vwap30": vwap,
        "volume_ratio20": rows[-1].volume / avg_volume if avg_volume else 0,
        "support20": min(r.low for r in rows[-20:]),
        "resistance20": max(r.high for r in rows[-20:]),
    }
