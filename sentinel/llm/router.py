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
RETRY_DELAYS = (5, 10)

# Transient capacity errors worth waiting out (429/529, quota, overload).
# Auth errors, bad requests, and bugs are NOT here: retrying them only
# makes the user wait for the same failure.
_RETRIABLE_MARKERS = (
    "429",
    "529",
    "503",
    "rate_limit",
    "rate limit",
    "too many requests",
    "overloaded",
    "resource_exhausted",
    "quota",
)


def _is_retriable(err):
    msg = str(err).lower()
    return any(marker in msg for marker in _RETRIABLE_MARKERS)

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


def generate_reply(user_text, ctx, history=None, on_busy=None, sleep=time.sleep):
    """Try each provider; if every available one is merely rate-limited,
    wait (RETRY_DELAYS) and try again instead of reporting both as down.
    on_busy(delay) lets the caller surface the wait to the user."""
    delays = list(RETRY_DELAYS)
    while True:
        rate_limited = False
        for breaker, ask in _PROVIDERS:
            if not breaker.allows():
                logger.info("Skipping %s: circuit open.", breaker.name)
                continue
            try:
                reply = ask(user_text, ctx, history)
            except Exception as err:
                breaker.record_failure()
                if _is_retriable(err):
                    rate_limited = True
                logger.warning(
                    "%s call failed (circuit %s): %s", breaker.name, breaker.state, err
                )
                continue
            # A None/empty reply (safety block, empty candidate, truncated tool loop)
            # is a failure too: it must trip the breaker, not reach Slack as text=None.
            if not (reply and reply.strip()):
                breaker.record_failure()
                logger.warning(
                    "%s returned an empty reply (circuit %s).",
                    breaker.name,
                    breaker.state,
                )
                continue
            breaker.record_success()
            return reply
        if not rate_limited or not delays:
            return BOTH_LLMS_DOWN
        delay = delays.pop(0)
        if on_busy:
            on_busy(delay)
        logger.info("All providers rate-limited; retrying in %ss.", delay)
        sleep(delay)
