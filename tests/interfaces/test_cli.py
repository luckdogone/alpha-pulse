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
