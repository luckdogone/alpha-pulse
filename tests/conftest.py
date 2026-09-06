import pytest

from alpha_pulse.config import Settings
from alpha_pulse.decision import DecisionTree
from alpha_pulse.demo import demo_snapshot
from alpha_pulse.evidence import EvidenceStore


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        artifacts_dir=tmp_path / "runs",
        http_proxy=None,
        https_proxy=None,
        all_proxy=None,
        binance_proxy=None,
    )


@pytest.fixture
def market_fixture(settings):
    store = EvidenceStore()
    snapshot = demo_snapshot(store, "long")
    # Convert this fixture's provenance for isolated freshness/guard tests.
    for record in store.records.values():
        record.source = "binance"
    return snapshot, store, DecisionTree(settings.tree_file())
