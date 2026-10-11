import asyncio

import anthropic

from agent.contracts import ASSESSMENT_SCHEMA, AgentError
from agent.prompts import SYSTEM_PROMPT
from config import Settings


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
