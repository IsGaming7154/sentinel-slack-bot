import pytest

from sentinel.config import BOTH_LLMS_DOWN
from sentinel.guard import GuardContext
from sentinel.llm import router
from sentinel.llm.router import CLOSED, DEGRADED, HALF_OPEN, OPEN, CircuitBreaker


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, secs):
        self.now += secs


@pytest.fixture
def clock():
    return FakeClock()


def test_breaker_opens_after_threshold(clock):
    b = CircuitBreaker("x", clock=clock)
    assert b.state == CLOSED
    for _ in range(router.FAILURE_THRESHOLD - 1):
        b.record_failure()
    assert b.state == DEGRADED
    assert b.allows()
    b.record_failure()
    assert b.state == OPEN
    assert not b.allows()


def test_breaker_half_opens_after_cooldown_and_closes_on_success(clock):
    b = CircuitBreaker("x", clock=clock)
    for _ in range(router.FAILURE_THRESHOLD):
        b.record_failure()
    clock.advance(router.COOLDOWN_SECS)
    assert b.state == HALF_OPEN
    assert b.allows()  # probe allowed
    b.record_success()
    assert b.state == CLOSED


def test_breaker_reopens_when_probe_fails(clock):
    b = CircuitBreaker("x", clock=clock)
    for _ in range(router.FAILURE_THRESHOLD):
        b.record_failure()
    clock.advance(router.COOLDOWN_SECS)
    b.record_failure()  # probe failed
    assert b.state == OPEN
    assert not b.allows()


def _patch_providers(monkeypatch, claude_fn, gemini_fn, clock):
    cb = CircuitBreaker("claude", clock=clock)
    gb = CircuitBreaker("gemini", clock=clock)
    monkeypatch.setattr(router, "_PROVIDERS", [(cb, claude_fn), (gb, gemini_fn)])
    return cb, gb


def test_failover_to_gemini(monkeypatch, clock):
    def bad_claude(text, ctx, history=None):
        raise RuntimeError("anthropic down")

    def good_gemini(text, ctx, history=None):
        return "gemini answer"

    cb, _ = _patch_providers(monkeypatch, bad_claude, good_gemini, clock)
    reply = router.generate_reply("hi", GuardContext(user_id="U1"))
    assert reply == "gemini answer"
    assert cb.state == DEGRADED


def test_both_down_returns_friendly_message(monkeypatch, clock):
    def bad(text, ctx, history=None):
        raise RuntimeError("down")

    _patch_providers(monkeypatch, bad, bad, clock)
    assert router.generate_reply("hi", GuardContext(user_id="U1")) == BOTH_LLMS_DOWN


def test_open_circuit_skips_provider(monkeypatch, clock):
    calls = {"claude": 0}

    def bad_claude(text, ctx, history=None):
        calls["claude"] += 1
        raise RuntimeError("down")

    def good_gemini(text, ctx, history=None):
        return "ok"

    _patch_providers(monkeypatch, bad_claude, good_gemini, clock)
    for _ in range(router.FAILURE_THRESHOLD):
        router.generate_reply("hi", GuardContext(user_id="U1"))
    assert calls["claude"] == router.FAILURE_THRESHOLD

    # circuit now open: claude must not be called again inside the cooldown
    router.generate_reply("hi", GuardContext(user_id="U1"))
    assert calls["claude"] == router.FAILURE_THRESHOLD

    # after the cooldown a probe goes through
    clock.advance(router.COOLDOWN_SECS)
    router.generate_reply("hi", GuardContext(user_id="U1"))
    assert calls["claude"] == router.FAILURE_THRESHOLD + 1


def test_rate_limited_providers_retry_and_recover(monkeypatch, clock):
    calls = {"n": 0}

    def flaky(text, ctx, history=None):
        calls["n"] += 1
        if calls["n"] <= 2:  # first pass: both providers rate-limited
            raise RuntimeError("Error code: 429 - rate_limit_error")
        return "answer"

    sleeps, busy = [], []
    _patch_providers(monkeypatch, flaky, flaky, clock)
    reply = router.generate_reply(
        "hi", GuardContext(user_id="U1"), on_busy=busy.append, sleep=sleeps.append
    )
    assert reply == "answer"
    assert sleeps == [router.RETRY_DELAYS[0]]
    assert busy == [router.RETRY_DELAYS[0]]


def test_non_retriable_failure_does_not_retry(monkeypatch, clock):
    def bad(text, ctx, history=None):
        raise RuntimeError("Error code: 401 - invalid x-api-key")

    sleeps = []
    _patch_providers(monkeypatch, bad, bad, clock)
    reply = router.generate_reply("hi", GuardContext(user_id="U1"), sleep=sleeps.append)
    assert reply == BOTH_LLMS_DOWN
    assert sleeps == []


def test_retries_exhausted_returns_down(monkeypatch, clock):
    def overloaded(text, ctx, history=None):
        raise RuntimeError("Error code: 529 - overloaded_error")

    sleeps = []
    _patch_providers(monkeypatch, overloaded, overloaded, clock)
    reply = router.generate_reply("hi", GuardContext(user_id="U1"), sleep=sleeps.append)
    assert reply == BOTH_LLMS_DOWN
    assert sleeps == list(router.RETRY_DELAYS)


def test_health_reports_states(monkeypatch, clock):
    def good(text, ctx, history=None):
        return "ok"

    cb, gb = _patch_providers(monkeypatch, good, good, clock)
    assert router.health() == {"claude": CLOSED, "gemini": CLOSED}
