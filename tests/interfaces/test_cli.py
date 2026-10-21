import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from domain.models import Prediction


def run_cli(tmp_path, *arguments, entry="module"):
    source = Path(__file__).resolve().parents[2] / "src"
    command = ["-m", "interfaces.cli"] if entry == "module" else [str(source / "__main__.py")]
    result = subprocess.run(
        [
            sys.executable,
            *command,
            "--env-file",
            str(tmp_path / "absent.env"),
            *arguments,
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(source.resolve()),
            "ARTIFACTS_DIR": str(tmp_path / "runs"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("entry", ["module", "source"])
def test_entry_point_exports_schema(tmp_path, entry):
    result = run_cli(tmp_path, "schema", entry=entry)
    assert json.loads(result.stdout) == Prediction.model_json_schema()


@pytest.mark.parametrize(
    "scenario,direction", [("long", "long"), ("short", "short"), ("ranging", "neutral")]
)
def test_demo_loads_bundled_policy_outside_repository(tmp_path, scenario, direction):
    output = tmp_path / "result.json"
    result = run_cli(tmp_path, "demo", "--scenario", scenario, "--output", str(output))
    prediction = Prediction.model_validate_json(result.stdout)
    assert prediction.engine == "demo"
    assert prediction.direction == direction
    assert json.loads(output.read_text()) == json.loads(result.stdout)
    archive = tmp_path / "runs" / prediction.run_id
    assert Prediction.model_validate_json((archive / "prediction.json").read_text()) == prediction
    assert (archive / "run.json").is_file()
    assert list((archive / "evidence").glob("*.json"))


async def test_watch_shares_live_state_with_analysis_and_closes_it(settings, monkeypatch):
    from argparse import Namespace

    from interfaces import cli

    states, results = [], []

    class Live:
        def __init__(self, binance, symbol, interval):
            assert symbol == "BTCUSDT" and interval == "15m"
            self.closed = False
            states.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def next_close(self):
            return {}

    async def analyze(configuration, symbol, interval, engine, *, live):
        assert live is states[0]
        return {"mode": engine}

    monkeypatch.setattr(cli, "LiveMarket", Live)
    monkeypatch.setattr(cli, "analyze", analyze)
    monkeypatch.setattr(cli, "emit", lambda value, **kwargs: results.append(value))
    await cli.watch(
        settings, None, Namespace(symbol="BTCUSDT", interval="15m", engine="rules", count=2)
    )
    assert states[0].closed and results == [{"mode": "rules"}, {"mode": "rules"}]
