import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}
INTERVALS = [*INTERVAL_MS, "1M"]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def normalize_symbol(value: str) -> str:
    value = value.strip().upper().replace("/", "").replace("-", "")
    if not re.fullmatch(r"[A-Z0-9]{2,30}", value):
        raise ValueError("symbol must be a Binance pair such as BTCUSDT")
    return value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Candle(Model):
    open_time: int = Field(ge=0)
    close_time: int = Field(ge=0)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    quote_volume: float = Field(ge=0)
    trades: int = Field(ge=0)
    taker_buy_volume: float = Field(ge=0)
    closed: bool

    @model_validator(mode="after")
    def consistent(self) -> "Candle":
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid OHLC ordering")
        if self.close_time < self.open_time or self.taker_buy_volume > self.volume * 1.000001:
            raise ValueError("invalid candle time/volume")
        return self

    @classmethod
    def from_rest(cls, row: list[Any], at: int) -> "Candle":
        return cls(
            open_time=row[0],
            open=row[1],
            high=row[2],
            low=row[3],
            close=row[4],
            volume=row[5],
            close_time=row[6],
            quote_volume=row[7],
            trades=row[8],
            taker_buy_volume=row[9],
            closed=row[6] < at,
        )

    @classmethod
    def from_ws(cls, row: dict[str, Any]) -> "Candle":
        return cls(
            open_time=row["t"],
            close_time=row["T"],
            open=row["o"],
            high=row["h"],
            low=row["l"],
            close=row["c"],
            volume=row["v"],
            quote_volume=row["q"],
            trades=row["n"],
            taker_buy_volume=row["V"],
            closed=row["x"],
        )


class Quality(Model):
    score: float = Field(ge=0, le=1)
    level: Literal["high", "medium", "low", "unavailable"]
    reasons: list[str]
    method: str = "source_freshness_completeness_v1; not a probability of truth"


class Evidence(Model):
    id: str
    tool: str
    source: str
    symbol: str
    status: Literal["ok", "partial", "unavailable", "error"] = "ok"
    fetched_at: int
    observed_at: int | None
    ttl_ms: int
    quality: Quality
    summary: dict[str, Any]
    data: Any = None
    error: str | None = None

    def compact(self) -> dict[str, Any]:
        return self.model_dump(exclude={"data"})


class Snapshot(Model):
    symbol: str
    interval: str
    as_of: int
    base_asset: str
    quote_asset: str
    tick_size: str
    candles: list[Candle]
    recent_1m: list[Candle]
    indicators: dict[str, Any]
    order_book: dict[str, Any]
    order_flow: dict[str, Any]
    derivatives: dict[str, Any]
    evidence_ids: list[str]
    required_ids: list[str]
    problems: list[str] = Field(default_factory=list)


class Assessment(Model):
    decision: Literal["long", "short", "no_trade"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=1600)
    evidence_ids: list[str] = Field(min_length=1, max_length=24)
    risks: list[str] = Field(default_factory=list, max_length=12)


class PriceZone(Model):
    low: float = Field(gt=0)
    high: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "PriceZone":
        if self.low > self.high:
            raise ValueError("zone low must be <= high")
        return self


class Prediction(Model):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    symbol: str
    market: Literal["binance_usdm_futures"] = "binance_usdm_futures"
    interval: str
    status: Literal["trade", "no_trade"]
    direction: Literal["long", "short", "neutral"]
    market_regime: Literal["trending", "ranging", "unknown"]
    reason_code: str
    generated_at: int
    data_as_of: int
    valid_from: int
    valid_until: int
    validity_seconds: int = Field(gt=0)
    reference_price: float | None = Field(default=None, gt=0)
    entry_zone: PriceZone | None = None
    take_profit_zone: PriceZone | None = None
    stop_loss_zone: PriceZone | None = None
    risk_reward_ratio: float | None = Field(default=None, gt=0)
    confidence: float = Field(default=0, ge=0, le=1)
    confidence_kind: Literal["uncalibrated_assessment"] = "uncalibrated_assessment"
    rationale: str
    risks: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    decision_path: list[dict[str, Any]] = Field(default_factory=list)
    engine: Literal["claude", "rules", "demo", "guard"]

    @model_validator(mode="after")
    def consistent(self) -> "Prediction":
        if self.valid_until - self.valid_from != self.validity_seconds * 1000:
            raise ValueError("validity does not match timestamps (UTC milliseconds)")
        if self.valid_from < self.generated_at or self.data_as_of > self.generated_at + 5000:
            raise ValueError("invalid prediction timestamps")
        zones = [self.entry_zone, self.take_profit_zone, self.stop_loss_zone]
        if self.status == "no_trade":
            if self.direction != "neutral" or any(zones) or self.risk_reward_ratio is not None:
                raise ValueError("no_trade must not have price zones or a direction")
        else:
            if not all(zones) or self.reference_price is None or self.risk_reward_ratio is None:
                raise ValueError("trade requires all zones, reference price, and risk/reward")
            entry, profit, stop = zones
            if self.direction == "long":
                valid = stop.high < entry.low <= entry.high < profit.low
            elif self.direction == "short":
                valid = profit.high < entry.low <= entry.high < stop.low
            else:
                valid = False
            if not valid:
                raise ValueError("price zones inconsistent with direction")
        return self
