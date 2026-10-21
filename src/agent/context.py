from agent.contracts import dumps
from agent.prompts import SYSTEM_PROMPT
from domain.decision import Decision
from domain.models import Candle, Snapshot
from storage.evidence import EvidenceStore


def candle_rows(candles: list[Candle]) -> list[list]:
    return [[c.open_time, c.open, c.high, c.low, c.close, c.volume, c.closed] for c in candles]


class ContextWindow:
    """Bound bytes, archive raw evidence, and compact only complete assistant/tool exchanges."""

    def __init__(
        self,
        snapshot: Snapshot,
        decision: Decision,
        store: EvidenceStore,
        max_bytes: int,
        tree: dict,
    ):
        self.snapshot, self.decision, self.store = snapshot, decision, store
        self.max_bytes, self.tree = max_bytes, tree
        self.exchanges: list[list[dict]] = []
        self.archived_exchanges = 0
        self.reserved_bytes = 0

    def messages(self, note: str = "") -> list[dict]:
        s = self.snapshot

        def qualified(value, source):
            state = s.data_quality.get("sources", {}).get(source, {}).get("status", "VALID")
            return value if state == "VALID" else {"quality_status": state}

        ledger = [
            {
                k: row[k]
                for k in ("id", "tool", "source", "symbol", "status", "observed_at", "quality")
            }
            | {
                "summary_index": {
                    k: row["summary"][k]
                    for k in (
                        "interval",
                        "requested_start",
                        "requested_end",
                        "count",
                        "metric",
                        "query",
                        "asset",
                        "frequency",
                        "exchange_list",
                        "total_liquidation_usd",
                    )
                    if k in row["summary"]
                }
            }
            for row in self.store.ledger()
        ]
        pinned = {
            "task": {"symbol": s.symbol, "interval": s.interval, "as_of": s.as_of},
            "decision_tree": self.tree,
            "tree_evaluation": self.decision.model_dump(),
            "market_snapshot": {
                "indicators": qualified(s.indicators, "get_klines"),
                "order_book": qualified(s.order_book, "get_order_book"),
                "order_flow": qualified(s.order_flow, "get_order_flow"),
                "derivatives": {
                    name: qualified(summary, "get_" + name)
                    for name, summary in s.derivatives.items()
                },
                "features": {
                    name: value
                    for name, value in s.features.items()
                    if s.feature_quality_status.get(name) == "VALID"
                },
                "feature_quality_status": s.feature_quality_status,
                "data_quality": s.data_quality,
                "feature_policy": (
                    "Only VALID features may support a claim. "
                    "STALE/MISSING/INVALID are unavailable, "
                    "never zero. CVD is base volume; OI changes are fractions; "
                    "funding history is settled, "
                    "current funding is published. Force-order amounts are observed samples only."
                ),
                "kline_columns": [
                    "open_time_ms",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "closed",
                ],
                "recent_1m": qualified(candle_rows(s.recent_1m), "get_recent_1m"),
                "recent_analysis_interval": qualified(candle_rows(s.candles[-8:]), "get_klines"),
                "problems": s.problems,
                "required_evidence_ids": s.required_ids,
            },
            "evidence_index": ledger,
            "working_note_untrusted": note,
            "archived_exchanges": self.archived_exchanges,
            "archive_policy": "raw evidence is retained; read_evidence can retrieve it by ID",
        }
        while True:
            pinned["archived_exchanges"] = self.archived_exchanges
            messages = [{"role": "user", "content": dumps(pinned)}]
            for exchange in self.exchanges:
                messages.extend(exchange)
            # UTF-8 bytes are an explicit deterministic budget, not a claimed token count.
            size = len(dumps(messages).encode()) + len(SYSTEM_PROMPT.encode()) + self.reserved_bytes
            if size <= self.max_bytes:
                return messages
            if not self.exchanges:
                raise ValueError(
                    "pinned context exceeds CONTEXT_MAX_BYTES; increase the configured budget"
                )
            self.exchanges.pop(0)
            self.archived_exchanges += 1

    def add_exchange(self, assistant_content: list[dict], tool_results: list[dict]):
        self.exchanges.append(
            [
                {"role": "assistant", "content": assistant_content},
                {"role": "user", "content": tool_results},
            ]
        )
