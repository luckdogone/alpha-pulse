import asyncio
import json
from pathlib import Path

import anthropic
from pydantic import ValidationError

from .config import Settings
from .context import SYSTEM_PROMPT, ContextWindow, dumps
from .models import Assessment
from .tools import ToolRegistry

ASSESSMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["long", "short", "no_trade"]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "confidence", "rationale", "evidence_ids", "risks"],
}


class AgentError(RuntimeError):
    pass


class ClaudeModel:
    provider_label = "Claude"

    def __init__(self, settings: Settings):
        self.settings = settings
        kwargs = {
            "base_url": settings.anthropic_base_url,
            "http_client": anthropic.DefaultAsyncHttpxClient(
                proxy=settings.proxy(), trust_env=False, timeout=settings.model_timeout_seconds
            ),
            "max_retries": 1,
        }
        if settings.anthropic_api_key:
            kwargs["api_key"] = settings.anthropic_api_key.get_secret_value()
        if settings.anthropic_auth_token:
            kwargs["auth_token"] = settings.anthropic_auth_token.get_secret_value()
        # Let the official SDK resolve an existing login profile when keys are absent.
        try:
            self.client = anthropic.AsyncAnthropic(**kwargs)
        except anthropic.AnthropicError:
            raise AgentError(
                "Claude authentication unavailable; configure ANTHROPIC_API_KEY or ant auth login"
            ) from None

    async def close(self):
        await self.client.close()

    def request_parameters(self, messages: list[dict], tools: list[dict], *, final: bool) -> dict:
        return {
            "model": self.settings.anthropic_model,
            "max_tokens": 8192,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "tools": tools,
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": self.settings.anthropic_effort,
                "format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA},
            },
            "tool_choice": {"type": "none" if final else "auto"},
        }

    def use_fallbacks(self) -> bool:
        return self.settings.anthropic_fallbacks and self.settings.anthropic_model in {
            "claude-opus-5",
            "claude-fable-5",
        }

    async def complete(self, messages: list[dict], tools: list[dict], *, final: bool):
        params = self.request_parameters(messages, tools, final=final)
        messages_api = self.client.messages
        if self.use_fallbacks():
            messages_api = self.client.beta.messages
            params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            async with asyncio.timeout(self.settings.model_timeout_seconds):
                async with messages_api.stream(**params) as stream:
                    return await stream.get_final_message()
        except anthropic.AuthenticationError:
            raise AgentError(f"{self.provider_label} authentication failed") from None
        except anthropic.PermissionDeniedError:
            raise AgentError(f"{self.provider_label} account lacks model/API permissions") from None
        except anthropic.NotFoundError:
            raise AgentError(
                f"{self.provider_label} model/endpoint not found; check model and base URL"
            ) from None
        except anthropic.RateLimitError:
            raise AgentError(f"{self.provider_label} rate limit exceeded") from None
        except anthropic.APIStatusError as exc:
            raise AgentError(
                f"{self.provider_label} HTTP {exc.status_code}; check model and API configuration"
            ) from None
        except (anthropic.APIConnectionError, TimeoutError):
            raise AgentError(
                f"{self.provider_label} connection failed or model timeout exceeded"
            ) from None


class DeepSeekModel(ClaudeModel):
    """DeepSeek's officially documented /anthropic API; requests go only to DeepSeek."""

    provider_label = "DeepSeek"

    def __init__(self, settings: Settings):
        self.settings = settings
        if settings.deepseek_api_key is None:
            raise AgentError("DeepSeek authentication unavailable; configure DEEPSEEK_API_KEY")
        self.client = anthropic.AsyncAnthropic(
            # SDK 1.4+ treats explicit credentials as complete; no Claude token is inherited.
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=settings.deepseek_base_url,
            http_client=anthropic.DefaultAsyncHttpxClient(
                proxy=settings.proxy(), trust_env=False, timeout=settings.model_timeout_seconds
            ),
            max_retries=1,
        )

    def use_fallbacks(self) -> bool:
        return False

    def request_parameters(self, messages: list[dict], tools: list[dict], *, final: bool) -> dict:
        # DeepSeek supports output_config.effort, but not output_config.format on this API.
        # Explicit instructions and shared Pydantic validation enforce the JSON contract.
        json_instructions = (
            "\nWhen you finish, output exactly one JSON object, without markdown fences. "
            "Use only the fields in this JSON Schema: "
            + dumps(ASSESSMENT_SCHEMA)
            + '\nExample shape: {"decision":"no_trade","confidence":0.5,'
            '"rationale":"证据不足。","evidence_ids":["an_existing_evidence_id"],"risks":[]}.'
            " Replace the example ID with real supplied evidence IDs. "
            "When more data is needed, use native tool calls before the final JSON."
        )
        return {
            "model": self.settings.deepseek_model,
            "max_tokens": 8192,
            "system": SYSTEM_PROMPT + json_instructions,
            "messages": messages,
            "tools": tools,
            "thinking": {"type": "enabled", "budget_tokens": 4096},
            "output_config": {"effort": self.settings.deepseek_effort},
            "tool_choice": {"type": "none" if final else "auto"},
        }


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
