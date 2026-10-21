import pytest

from data.quality.scoring import assess_quality


def test_quality_uses_observation_not_fetch_time():
    fresh = assess_quality("binance", 100_000, 10_000, "ok", at=101_000)
    stale = assess_quality("binance", 1, 10_000, "ok", at=101_000)
    missing = assess_quality("coinglass", None, 10_000, "unavailable", at=101_000)
    future = assess_quality("binance", 120_000, 10_000, "ok", at=101_000)
    assert fresh.score > stale.score
    assert missing.score == future.score == 0


@pytest.mark.parametrize(
    "observed, status, expected",
    [
        (90_000, "ok", 0.95),
        (89_999, "ok", 0.237),
        (105_000, "ok", 0.95),
        (105_001, "ok", 0),
        (None, "ok", 0.475),
        (100_000, "partial", 0.57),
        (100_000, "error", 0),
    ],
)
def test_quality_boundaries_preserve_freshness_and_partial_penalties(observed, status, expected):
    assert assess_quality("binance", observed, 10_000, status, at=100_000).score == expected
