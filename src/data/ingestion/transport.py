import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from email.utils import parsedate_to_datetime

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from data.errors import DataError
from domain.models import now_ms

log = logging.getLogger(__name__)


async def stream_json(
    urls: list[str],
    *,
    proxy: str | None,
    open_timeout: float,
    decode: Callable[[dict], dict | None],
    stream_name: str,
) -> AsyncIterator[dict]:
    """Reconnect boundedly; only accepted messages reset consecutive failures.

    The provider decoder owns protocol validation and replay filtering. Its state
    survives reconnects, while this transport owns sockets, timeouts and backoff.
    """
    failures = 0
    while True:
        url = urls[min(failures, len(urls) - 1)]
        try:
            async with connect(
                url,
                proxy=proxy,
                open_timeout=open_timeout,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=3,
                max_size=1_000_000,
                max_queue=16,
            ) as socket:
                while True:
                    raw = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
                    event = decode(raw)
                    if event is None:
                        continue
                    if failures:
                        log.info("%s stream reconnected", stream_name)
                    failures = 0
                    yield event
        except (OSError, TimeoutError, WebSocketException, ValueError, KeyError) as exc:
            failures += 1
            log.warning(
                "%s stream interrupted (%s), attempt %d/6",
                stream_name,
                type(exc).__name__,
                failures,
            )
            if failures >= 6:
                raise DataError(
                    f"{stream_name} stream unavailable after 6 reconnect attempts"
                ) from None
            await asyncio.sleep(min(2 ** (failures - 1), 10))


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
