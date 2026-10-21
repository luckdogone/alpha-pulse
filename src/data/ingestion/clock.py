"""Server clock calibration using the midpoint of the lowest-RTT sample."""

import asyncio
from time import monotonic

from data.errors import DataError
from domain.models import now_ms


class ServerClock:
    def __init__(self, refresh_seconds: int, samples: int):
        self.refresh_seconds, self.samples = refresh_seconds, samples
        self.offset_ms = 0
        self.rtt_ms: int | None = None
        self.calibrated_at: int | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    def now(self) -> int:
        return now_ms() + self.offset_ms

    async def calibrate(self, get, *, force: bool = False) -> dict:
        async with self._lock:
            if force or monotonic() >= self._expires:
                samples = []
                for _ in range(self.samples):
                    before = now_ms()
                    server = int((await get("/fapi/v1/time"))["serverTime"])
                    after = now_ms()
                    if server <= 0 or after < before:
                        raise DataError("invalid server clock sample")
                    samples.append((after - before, server - (before + after) // 2))
                self.rtt_ms, self.offset_ms = min(samples, key=lambda sample: sample[0])
                if self.rtt_ms > 5000:
                    raise DataError("server clock calibration round trip exceeds 5 seconds")
                self.calibrated_at = self.now()
                self._expires = monotonic() + self.refresh_seconds
        return {
            "server_time_offset_ms": self.offset_ms,
            "round_trip_ms": self.rtt_ms,
            "calibrated_at": self.calibrated_at,
        }
