from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    anthropic_api_key: SecretStr | None = None
    anthropic_auth_token: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: str = "high"
    anthropic_fallbacks: bool = True
    http_proxy: str | None = None
    https_proxy: str | None = None
    all_proxy: str | None = None
    binance_proxy: str | None = None
    binance_rest_base_url: str = "https://fapi.binance.com"
    binance_ws_url_template: str = "wss://fstream.binance.com/market/ws/{symbol}@kline_{interval}"
    binance_ws_fallback_url_template: str | None = None
    coinglass_api_key: SecretStr | None = None
    coinglass_base_url: str = "https://open-api-v4.coinglass.com"
    coinglass_exchanges: str = "Binance,OKX,Bybit"
    onchain_url_template: str | None = None
    onchain_api_key: SecretStr | None = None
    coinmetrics_base_url: str = "https://community-api.coinmetrics.io/v4"
    news_domains: str = "reuters.com,apnews.com,bloomberg.com,ft.com,bbc.com,coindesk.com"
    snapshot_minutes: int = Field(default=30, ge=30, le=120)
    indicator_warmup_candles: int = Field(default=250, ge=60, le=1000)
    analysis_validity_minutes: int = Field(default=30, ge=1, le=120)
    request_timeout_seconds: float = Field(default=10, ge=1, le=60)
    analysis_timeout_seconds: float = Field(default=180, ge=10, le=600)
    model_timeout_seconds: float = Field(default=90, ge=5, le=300)
    max_agent_rounds: int = Field(default=6, ge=1, le=16)
    max_tool_calls: int = Field(default=16, ge=1, le=64)
    context_max_bytes: int = Field(default=48000, ge=16000, le=200000)
    decision_tree_path: Path | None = None
    artifacts_dir: Path = Path("runs")

    @field_validator("http_proxy", "https_proxy", "all_proxy", "binance_proxy")
    @classmethod
    def valid_proxy(cls, value: str | None) -> str | None:
        if value and (
            urlsplit(value).scheme not in {"http", "https", "socks5", "socks5h"}
            or not urlsplit(value).hostname
        ):
            raise ValueError("proxy must be an http(s) or socks5(h) URL")
        return value

    @field_validator("anthropic_effort")
    @classmethod
    def valid_effort(cls, value: str) -> str:
        if value not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("unsupported model effort")
        return value

    def proxy(self, *, binance: bool = False) -> str | None:
        return (self.binance_proxy if binance else None) or (
            self.https_proxy or self.http_proxy or self.all_proxy
        )

    def tree_file(self) -> Path:
        return self.decision_tree_path or Path(__file__).with_name("default_decision_tree.json")
