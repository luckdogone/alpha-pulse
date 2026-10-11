import anthropic

from agent.contracts import ASSESSMENT_SCHEMA, AgentError, dumps
from agent.prompts import SYSTEM_PROMPT
from agent.providers.claude import ClaudeModel
from config import Settings


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
