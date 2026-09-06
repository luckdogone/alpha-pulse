import asyncio
from email.utils import parsedate_to_datetime

import httpx

from .models import now_ms


class DataError(RuntimeError):
    """Sanitized external-data failure; never contains credentials or response bodies."""


class JsonHTTP:
    def __init__(
        self,
        proxy: str | None,
        timeout: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.client = httpx.AsyncClient(
            proxy=proxy,
            timeout=timeout,
            trust_env=False,
            transport=transport,
            follow_redirects=False,
        )
        self.slots = asyncio.Semaphore(4)

    async def close(self):
        await self.client.aclose()

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None):
        for attempt in range(3):
            try:
                async with self.slots:
                    response = await self.client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError) as exc:
                if attempt == 2:
                    raise DataError(
                        f"network failure ({type(exc).__name__}); check proxy"
                    ) from None
                await asyncio.sleep(0.25 * 2**attempt)
                continue
            if response.status_code in {418, 429} or response.status_code >= 500:
                retry_after = response.headers.get("retry-after", "1")
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(retry_after).timestamp() - now_ms() / 1000
                    except (ValueError, TypeError):
                        delay = 1
                # Long rate limits are returned to the caller, never retried early.
                if attempt == 2 or delay > 5 or response.status_code == 418:
                    raise DataError(
                        f"HTTP {response.status_code}; retry_after_seconds={max(delay, 0):g}"
                    )
                await asyncio.sleep(max(delay, 0.25 * 2**attempt))
                continue
            if response.status_code >= 300:
                raise DataError(
                    f"HTTP {response.status_code}; verify endpoint, access, and API plan"
                )
            try:
                return response.json()
            except ValueError:
                raise DataError("provider returned non-JSON data") from None
        raise DataError("request retries exhausted")
