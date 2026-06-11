import pytest

from sentinel import guard, memory
from sentinel.guard import GuardContext
from sentinel.handlers.replies import reply_blocks, trace_line


@pytest.fixture(autouse=True)
def clean_memory():
    memory._conversations.clear()
    yield
    memory._conversations.clear()


def test_memory_roundtrip():
    assert memory.history("t1") == []
    memory.remember("t1", "hi", "hello")
    assert memory.history("t1") == [("user", "hi"), ("assistant", "hello")]


def test_memory_is_isolated_per_key():
    memory.remember("t1", "a", "b")
    assert memory.history("t2") == []


def test_memory_caps_turns():
    for i in range(memory.MAX_TURNS + 5):
        memory.remember("t1", "q{}".format(i), "a{}".format(i))
    turns = memory.history("t1")
    assert len(turns) == 2 * memory.MAX_TURNS
    assert turns[-1] == ("assistant", "a{}".format(memory.MAX_TURNS + 4))


def test_memory_ignores_empty_key():
    memory.remember(None, "q", "a")
    assert memory.history(None) == []


def test_trace_line_summarizes_decisions():
    ctx = GuardContext(user_id="U1", provider="claude")
    ctx.tool_calls = [
        ("read_query", guard.ALLOW),
        ("read_query", guard.ALLOW),
        ("write_query", guard.QUEUE),
        ("evil_tool", guard.BLOCK),
    ]
    line = trace_line(ctx)
    assert "Claude" in line
    assert "2 reads" in line
    assert "1 write queued for approval" in line
    assert "1 call blocked" in line


def test_trace_line_empty_when_nothing_happened():
    ctx = GuardContext(user_id="U1")
    assert trace_line(ctx) == ""


def test_reply_blocks_have_footer_and_chunking():
    ctx = GuardContext(user_id="U1", provider="gemini")
    ctx.tool_calls = [("read_query", guard.ALLOW)]
    blocks = reply_blocks("x" * 6000, ctx)
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == 3  # 6000 chars / 2900 per section
    assert blocks[-1]["type"] == "context"
    assert "Gemini" in blocks[-1]["elements"][0]["text"]


def test_guard_execute_records_trace(tmp_db):
    ctx = GuardContext(user_id="U1")
    guard.execute("write_query", {"query": "DELETE FROM tickets"}, ctx)
    guard.execute("not_a_tool", {}, ctx)
    assert ctx.tool_calls == [
        ("write_query", guard.QUEUE),
        ("not_a_tool", guard.BLOCK),
    ]
