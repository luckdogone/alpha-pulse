import json

import pytest

from domain.models import now_ms
from storage.evidence import EvidenceStore


def test_assembled_evidence_persists_pages_and_reassesses_freshness(tmp_path):
    store = EvidenceStore(tmp_path)
    at = now_ms()
    record = store.add(
        tool="get_order_flow",
        source="binance",
        symbol="BTCUSDT",
        observed_at=at,
        ttl_ms=1000,
        summary={"count": 3},
        data={"trades": [1, 2, 3]},
    )
    archived = json.loads((tmp_path / f"{record.id}.json").read_text())
    assert archived == record.model_dump(mode="json")
    assert store.page(record.id, field="trades", limit=2) == {
        "evidence_id": record.id,
        "total": 3,
        "offset": 0,
        "next_offset": 2,
        "data": [1, 2],
    }
    assert store.page(record.id, field="trades", offset=2)["next_offset"] is None
    assert store.ledger(at=at)[0]["quality"]["score"] == 0.95
    assert store.ledger(at=at + 1001)[0]["quality"]["score"] == 0.237
    assert record.quality.score == 0.95


@pytest.mark.parametrize("field", ["data", "summary"])
def test_nonfinite_payload_never_enters_store(tmp_path, field):
    store = EvidenceStore(tmp_path)
    kwargs = dict(
        tool="get_test",
        source="binance",
        symbol="BTCUSDT",
        observed_at=None,
        ttl_ms=1000,
        summary={},
    )
    kwargs[field] = {"nested": [float("nan")]}
    with pytest.raises(ValueError):
        store.add(**kwargs)
    assert not store.records
    assert not list(tmp_path.iterdir())
