from decimal import Decimal

import pytest
from pydantic import ValidationError

from application.service import analyze
from domain.models import Prediction


@pytest.mark.parametrize(
    "scenario,direction", [("long", "long"), ("short", "short"), ("ranging", "no_trade")]
)
async def test_demo_contracts(settings, scenario, direction):
    result = await analyze(settings, engine="demo", scenario=scenario)
    assert result.direction == (direction if direction != "no_trade" else "neutral")
    assert result.engine == "demo"
    assert result.status == ("trade" if direction != "no_trade" else "no_trade")
    assert result.valid_until - result.valid_from == 1_800_000
    assert Prediction.model_validate_json(result.model_dump_json()) == result
    if direction == "no_trade":
        assert result.market_regime == "ranging"
        assert result.entry_zone is result.take_profit_zone is result.stop_loss_zone is None
    else:
        assert result.risk_reward_ratio >= 1.5
        for zone in (result.entry_zone, result.take_profit_zone, result.stop_loss_zone):
            for price in (zone.low, zone.high):
                assert Decimal(str(price)) % Decimal("0.001") == 0
        with pytest.raises(ValidationError):
            Prediction.model_validate(result.model_dump() | {"direction": "neutral"})
