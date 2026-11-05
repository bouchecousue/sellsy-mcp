"""Process-wide read admission and cooldown, without retaining customer data."""

from __future__ import annotations

import asyncio
import math
import random
import time
from contextlib import contextmanager

from .errors import ToolError


class ReadBackpressure:
    def __init__(self, limit: int = 64):
        self.limit = limit
        self.pending = 0
        self.cooldown_until = 0.0
        self.paced_until = 0.0
        self.next_start = 0.0
        self.lock = asyncio.Lock()

    def reject(self, reason: str, delay: float = 1) -> ToolError:
        return ToolError(
            "rate_limited",
            "Les lectures sont temporairement limitées ; attendre avant une nouvelle demande.",
            rate_limit_reason=reason,
            retry_after_seconds=max(1, math.ceil(delay)),
            next_action="Réduire les lectures parallèles et attendre le délai indiqué.",
        )

    @contextmanager
    def admission(self):
        if self.pending >= self.limit:
            raise self.reject("read_queue_full", self.cooldown_until - time.monotonic())
        self.pending += 1
        try:
            yield
        finally:
            self.pending -= 1

    def defer(self, delay: float) -> None:
        # Never shorten a cooldown learned from another request, including a final attempt.
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
        self.paced_until = max(self.paced_until, self.cooldown_until + 60)

    async def wait(self, deadline: float) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                delay = max(self.cooldown_until, self.next_start) - now
                if delay <= 0:
                    if now >= deadline:
                        raise self.reject("read_budget_exhausted")
                    if now < self.paced_until:
                        # A conservative recovery pace, not a claim about Sellsy's quota.
                        # Scheduling jitter only; never used for tokens, keys or authorization.
                        self.next_start = now + 0.25 + random.uniform(0, 0.05)  # nosec B311
                    return
                if now + delay >= deadline:
                    raise self.reject("shared_cooldown", delay)
                await asyncio.sleep(delay)
                # An in-flight request may have extended the cooldown while we slept.
