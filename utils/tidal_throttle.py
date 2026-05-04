# TIDAL download pacing helpers.
# Throttling is toggled per-module in settings.json: modules.tidal.throttle (boolean, default true).
# When enabled: random 3–5 s between track download starts and ~60 API metadata calls/minute (sliding window).
# Official TIDAL docs do not publish a numeric rate limit; the RPM cap is a conservative safeguard.

from __future__ import annotations

import asyncio
import random
import threading
import time
from typing import Any, Dict, Optional


def _parse_throttle_flag(raw: Any) -> bool:
    """Default True when absent (backward compatible)."""
    if raw is None:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return bool(raw)


def resolve_tidal_throttle(
    full_settings: Optional[Dict[str, Any]], service_name: Optional[str]
) -> Optional[Dict[str, Any]]:
    """
    Returns pacing parameters dict if TIDAL throttling is active, else None (legacy unrestricted behavior).
    Reads modules.tidal.throttle from full_settings (same shape as Orpheus settings.json).
    """
    if not service_name or str(service_name).lower() != "tidal":
        return None
    tidal: Dict[str, Any] = {}
    if full_settings and isinstance(full_settings.get("modules"), dict):
        mod = full_settings["modules"].get("tidal")
        if isinstance(mod, dict):
            tidal = mod
    if not _parse_throttle_flag(tidal.get("throttle", True)):
        return None
    return {"delay_min": 3.0, "delay_max": 5.0, "rpm": 60}


class TidalInterTrackGateSync:
    """Serialize track download starts with a random gap (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait_turn(self, delay_min: float, delay_max: float) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_start:
                time.sleep(self._next_start - now)
            gap = random.uniform(delay_min, delay_max)
            self._next_start = time.monotonic() + gap


class TidalInterTrackGateAsync:
    """Serialize track download starts with a random gap (async)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def wait_turn(self, delay_min: float, delay_max: float) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next_start:
                await asyncio.sleep(self._next_start - now)
            gap = random.uniform(delay_min, delay_max)
            self._next_start = time.monotonic() + gap


class RequestsPerMinuteLimiterSync:
    """Sliding-window limiter for counted operations (thread-safe)."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max(0, int(max_per_minute))
        self._lock = threading.Lock()
        self._timestamps: list[float] = []

    def acquire(self) -> None:
        if self.max_per_minute <= 0:
            return
        window = 60.0
        while True:
            sleep_for = 0.0
            with self._lock:
                now = time.monotonic()
                self._timestamps = [t for t in self._timestamps if now - t < window]
                if len(self._timestamps) < self.max_per_minute:
                    self._timestamps.append(time.monotonic())
                    return
                sleep_for = max(0.0, self._timestamps[0] + window - now)
            if sleep_for > 0:
                time.sleep(sleep_for)


class RequestsPerMinuteLimiterAsync:
    """Sliding-window limiter for counted operations (async)."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max(0, int(max_per_minute))
        self._lock = asyncio.Lock()
        self._timestamps: list[float] = []

    async def acquire(self) -> None:
        if self.max_per_minute <= 0:
            return
        window = 60.0
        while True:
            sleep_for = 0.0
            async with self._lock:
                now = time.monotonic()
                self._timestamps = [t for t in self._timestamps if now - t < window]
                if len(self._timestamps) < self.max_per_minute:
                    self._timestamps.append(time.monotonic())
                    return
                sleep_for = max(0.0, self._timestamps[0] + window - now)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
