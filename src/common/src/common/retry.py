"""Retry settings and backoff calculation."""

import random

from pydantic import Field

from .durations import Duration
from .settings import StrictModel


class RetryConfig(StrictModel):
    attempts: int = Field(default=3, ge=1, le=100)
    initial_delay: Duration = 2.0
    max_delay: Duration = 60.0
    multiplier: float = Field(default=2.0, ge=1.0)
    jitter: float = Field(default=0.1, ge=0.0, le=1.0)

    def delay(self, attempt: int) -> float:
        """Delay before retry number `attempt` (1-based: delay after first failure)."""
        base = min(self.max_delay, self.initial_delay * (self.multiplier ** (attempt - 1)))
        if self.jitter:
            base *= 1 + random.uniform(-self.jitter, self.jitter)
        return max(0.0, base)
