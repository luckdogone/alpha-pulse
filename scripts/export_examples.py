"""Regenerate standalone JSON examples from synthetic data; no network or model calls."""

import asyncio
import json
from pathlib import Path

from alpha_pulse.config import Settings
from alpha_pulse.models import Prediction
from alpha_pulse.service import analyze


async def main():
    output = Path("examples")
    await asyncio.to_thread(output.mkdir, exist_ok=True)
    settings = Settings(_env_file=None)
    for scenario in ("long", "short", "ranging"):
        result = await analyze(settings, engine="demo", scenario=scenario)
        path = output / f"prediction.{scenario}.json"
        path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"{path}: {result.status}/{result.direction}")
    (output / "prediction.schema.json").write_text(
        json.dumps(Prediction.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
