import json
import math
import operator
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .evidence import EvidenceStore
from .models import Model, PriceZone, Snapshot, now_ms

FEATURES = {
    "spread_bps",
    "min_book_notional",
    "atr_pct",
    "trend_strength_atr",
    "abs_funding_rate",
    "ema_alignment",
    "macd_histogram",
    "rsi14",
    "buy_ratio",
    "imbalance",
    "bollinger_width_pct",
    "volume_ratio20",
}
OPERATORS = {
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
    "eq": operator.eq,
}


class Condition(Model):
    field: str
    op: Literal["gt", "ge", "lt", "le", "eq"]
    value: float

    @model_validator(mode="after")
    def known_field(self):
        if self.field not in FEATURES:
            raise ValueError(f"unknown decision feature: {self.field}")
        return self


class Gate(Model):
    id: str
    reason_code: str
    conditions: list[Condition] = Field(min_length=1)


class RiskConfig(Model):
    minimum_confidence: float = Field(ge=0, le=1)
    min_risk_reward: float = Field(ge=1)
    target_risk_reward: float = Field(ge=1)
    stop_atr: float = Field(gt=0, le=10)
    entry_half_width_atr: float = Field(gt=0, le=1)
    stop_width_atr: float = Field(gt=0, le=2)
    profit_width_atr: float = Field(gt=0, le=5)
    fee_bps_per_side: float = Field(ge=0, le=100)
    slippage_bps_per_side: float = Field(ge=0, le=100)
    max_reference_drift_atr: float = Field(gt=0, le=5)


class TreeConfig(Model):
    version: str
    gates: list[Gate]
    long: list[Condition] = Field(min_length=1)
    short: list[Condition] = Field(min_length=1)
    risk: RiskConfig


class Decision(Model):
    direction: Literal["long", "short", "no_trade"]
    reason_code: str
    path: list[dict]
    features: dict


class DecisionTree:
    def __init__(self, path: Path):
        self.config = TreeConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def evaluate(
        self,
        snapshot: Snapshot,
        store: EvidenceStore,
        at: int | None = None,
        *,
        synthetic: bool = False,
    ) -> Decision:
        at = at or now_ms()
        problems = list(snapshot.problems)
        if not snapshot.required_ids:
            problems.append("no required evidence")
        for evidence_id in snapshot.required_ids:
            record = store.records.get(evidence_id)
            if record is None or record.status != "ok":
                problems.append(f"missing or incomplete evidence: {evidence_id}")
            elif (
                record.observed_at is None
                or at - record.observed_at > record.ttl_ms
                or record.observed_at > at + 5000
            ):
                problems.append(f"stale/unknown timestamp: {evidence_id}")
            elif record.source == "synthetic" and not synthetic:
                problems.append("synthetic data is not eligible for live analysis")
        path = [{"node": "data_quality", "passed": not problems, "problems": problems}]
        if problems:
            return Decision(
                direction="no_trade", reason_code="insufficient_data", path=path, features={}
            )
        features = {k: v for k, v in snapshot.indicators.items() if k in FEATURES}
        features.update(
            spread_bps=snapshot.order_book.get("spread_bps"),
            min_book_notional=min(
                snapshot.order_book.get("bid_notional", 0),
                snapshot.order_book.get("ask_notional", 0),
            ),
            imbalance=snapshot.order_book.get("imbalance"),
            buy_ratio=snapshot.order_flow.get("buy_ratio"),
        )
        try:
            features["abs_funding_rate"] = abs(
                float(snapshot.derivatives["mark_price"]["lastFundingRate"])
            )
        except (KeyError, TypeError, ValueError):
            features["abs_funding_rate"] = None

        def check(conditions: list[Condition]) -> tuple[bool, list[dict]]:
            checks = []
            for cond in conditions:
                value = features.get(cond.field)
                passed = (
                    isinstance(value, (int, float))
                    and math.isfinite(value)
                    and OPERATORS[cond.op](value, cond.value)
                )
                checks.append({**cond.model_dump(), "actual": value, "passed": passed})
            return all(c["passed"] for c in checks), checks

        for gate in self.config.gates:
            passed, checks = check(gate.conditions)
            path.append({"node": gate.id, "passed": passed, "checks": checks})
            if not passed:
                return Decision(
                    direction="no_trade", reason_code=gate.reason_code, path=path, features=features
                )
        matches = []
        for direction in ("long", "short"):
            passed, checks = check(getattr(self.config, direction))
            path.append({"node": direction, "passed": passed, "checks": checks})
            if passed:
                matches.append(direction)
        direction = matches[0] if len(matches) == 1 else "no_trade"
        return Decision(
            direction=direction,
            reason_code="setup_confirmed" if len(matches) == 1 else "conflicting_signals",
            path=path,
            features=features,
        )

    def zones(self, snapshot: Snapshot, direction: str) -> dict:
        if direction not in {"long", "short"}:
            raise ValueError("zones require a directional decision")
        risk = self.config.risk
        mid, atr = snapshot.order_book["mid_price"], snapshot.indicators["atr14"]
        tick = Decimal(snapshot.tick_size)
        if tick <= 0 or atr <= 0 or not math.isfinite(mid + atr):
            raise ValueError("invalid tick size, price, or ATR")

        def rounded(value: float, up: bool = False) -> float:
            return float(
                (Decimal(str(value)) / tick).to_integral_value(
                    rounding=ROUND_CEILING if up else ROUND_FLOOR
                )
                * tick
            )

        width = max(atr * risk.entry_half_width_atr, float(tick))
        entry = PriceZone(low=rounded(mid - width), high=rounded(mid + width, True))
        cost = mid * 2 * (risk.fee_bps_per_side + risk.slippage_bps_per_side) / 10000
        ratio = max(risk.target_risk_reward, risk.min_risk_reward)
        distance = max(atr * risk.stop_atr, float(tick) * 2)
        if direction == "long":
            stop = PriceZone(
                low=rounded(entry.low - distance - atr * risk.stop_width_atr),
                high=rounded(entry.low - distance, True),
            )
            worst_risk = entry.high - stop.low + cost
            nearest = rounded(entry.high + ratio * worst_risk + cost, True)
            profit = PriceZone(
                low=nearest, high=rounded(nearest + atr * risk.profit_width_atr, True)
            )
            actual_ratio = (profit.low - entry.high - cost) / worst_risk
        else:
            stop = PriceZone(
                low=rounded(entry.high + distance),
                high=rounded(entry.high + distance + atr * risk.stop_width_atr, True),
            )
            worst_risk = stop.high - entry.low + cost
            nearest = rounded(entry.low - ratio * worst_risk - cost)
            profit = PriceZone(low=rounded(nearest - atr * risk.profit_width_atr), high=nearest)
            actual_ratio = (entry.low - profit.high - cost) / worst_risk
        if actual_ratio < risk.min_risk_reward:
            raise ValueError("rounded zones fail minimum net risk/reward")
        return {
            "reference_price": mid,
            "entry_zone": entry,
            "take_profit_zone": profit,
            "stop_loss_zone": stop,
            "risk_reward_ratio": round(actual_ratio, 6),
        }
