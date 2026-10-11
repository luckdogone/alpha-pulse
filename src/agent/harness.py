import asyncio
import json
from pathlib import Path

from pydantic import ValidationError

from agent.context import ContextWindow
from agent.contracts import ASSESSMENT_SCHEMA, AgentError, dumps
from agent.tools import ToolRegistry
from domain.models import Assessment


class AgentHarness:
    def __init__(
        self,
        model,
        registry: ToolRegistry,
        context: ContextWindow,
        max_rounds: int,
        trace_path: Path | None = None,
    ):
        self.model, self.registry, self.context = model, registry, context
        self.max_rounds, self.trace_path = max_rounds, trace_path
        self.trace: list[dict] = []

    def record(self, row: dict):
        self.trace.append(row)
        if self.trace_path:
            # Only tool inputs/results and usage: never log credentials or private thinking blocks.
            with self.trace_path.open("a", encoding="utf-8") as file:
                file.write(dumps(row) + "\n")

    async def run(self) -> Assessment:
        repair = None
        tools = self.registry.definitions()
        # Include schemas and space for validation feedback in the request budget.
        self.context.reserved_bytes = (
            len(dumps(tools).encode()) + len(dumps(ASSESSMENT_SCHEMA).encode()) + 512
        )
        for round_number in range(self.max_rounds):
            final = (
                round_number == self.max_rounds - 1
                or self.registry.calls >= self.registry.max_calls
            )
            messages = self.context.messages(self.registry.note)
            if repair:
                messages[0]["content"] += "\nAssessment validation feedback: " + repair
            response = await self.model.complete(messages, tools, final=final)
            self.record(
                {
                    "round": round_number + 1,
                    "stop_reason": response.stop_reason,
                    "model": response.model,
                    "usage": response.usage.model_dump(),
                    "context_bytes": len(dumps(messages).encode()),
                    "compacted_exchanges": self.context.archived_exchanges,
                }
            )
            if response.stop_reason in {"refusal", "max_tokens"}:
                raise AgentError(f"model stopped with {response.stop_reason}")
            calls = [b for b in response.content if b.type == "tool_use"]
            if calls:
                if final:
                    raise AgentError("model requested tools after finalization budget")
                results = await asyncio.gather(
                    *(self.registry.call(b.name, b.input) for b in calls)
                )
                tool_results = []
                for call, (result, failed) in zip(calls, results, strict=True):
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": dumps(result),
                            "is_error": failed,
                        }
                    )
                    self.record({"tool": call.name, "input": call.input, "result": result})
                # Retain complete SDK blocks, including signed thinking, within each exchange.
                self.context.add_exchange(
                    [b.model_dump(exclude_none=True) for b in response.content], tool_results
                )
                continue
            if response.stop_reason != "end_turn":
                raise AgentError(f"unexpected model stop reason: {response.stop_reason}")
            text = "".join(b.text for b in response.content if b.type == "text")
            try:
                assessment = Assessment.model_validate(json.loads(text))
                for evidence_id in assessment.evidence_ids:
                    record = self.registry.store.records.get(evidence_id)
                    if record is None or record.status in {"unavailable", "error"}:
                        raise ValueError("unknown or unavailable supporting evidence ID")
                if not set(assessment.evidence_ids).intersection(
                    self.context.snapshot.required_ids
                ):
                    raise ValueError("assessment must cite required market evidence")
                self.record({"assessment": assessment.model_dump()})
                return assessment
            except (ValueError, ValidationError):
                repair = (
                    "Return valid assessment JSON and cite existing successful market evidence IDs."
                )
        raise AgentError("agent round budget exhausted without a valid assessment")
