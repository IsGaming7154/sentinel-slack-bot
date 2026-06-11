"""Provider routing with per-provider circuit breakers.

A provider that keeps failing is skipped for a cooldown period instead of
adding its timeout to every request. After the cooldown one probe call is
let through (half-open); a success closes the circuit again.
"""

import logging
import threading
import time

from sentinel.config import BOTH_LLMS_DOWN
from sentinel.llm.claude import ask_claude
from sentinel.llm.gemini import ask_gemini

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 3
COOLDOWN_SECS = 120

CLOSED = "closed"
DEGRADED = "degraded"
OPEN = "open"
HALF_OPEN = "half-open"


class CircuitBreaker:
    def __init__(self, name, clock=time.monotonic):
        self.name = name
        self._clock = clock
        self._failures = 0
        self._opened_at = None
        self._lock = threading.Lock()

    def allows(self):
        with self._lock:
            if self._opened_at is None:
                return True
            # After the cooldown, let a probe through (half-open).
            return self._clock() - self._opened_at >= COOLDOWN_SECS

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= FAILURE_THRESHOLD:
                self._opened_at = self._clock()

    @property
    def state(self):
        with self._lock:
            if self._opened_at is None:
                return CLOSED if self._failures == 0 else DEGRADED
            if self._clock() - self._opened_at >= COOLDOWN_SECS:
                return HALF_OPEN
            return OPEN


claude_breaker = CircuitBreaker("claude")
gemini_breaker = CircuitBreaker("gemini")

_PROVIDERS = [(claude_breaker, ask_claude), (gemini_breaker, ask_gemini)]


def health():
    return {breaker.name: breaker.state for breaker, _ in _PROVIDERS}


def generate_reply(user_text, ctx, history=None):
    for breaker, ask in _PROVIDERS:
        if not breaker.allows():
            logger.info("Skipping %s: circuit open.", breaker.name)
            continue
        try:
            reply = ask(user_text, ctx, history)
        except Exception as err:
            breaker.record_failure()
            logger.warning(
                "%s call failed (circuit %s): %s", breaker.name, breaker.state, err
            )
            continue
        breaker.record_success()
        return reply
    return BOTH_LLMS_DOWN
