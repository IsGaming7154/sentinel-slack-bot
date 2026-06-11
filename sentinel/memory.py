"""Per-conversation memory: the last few exchanges, keyed by thread/channel."""

import threading

MAX_TURNS = 10  # user+assistant exchanges kept per conversation
MAX_CONVERSATIONS = 500

_lock = threading.Lock()
_conversations = {}


def history(key):
    if not key:
        return []
    with _lock:
        return list(_conversations.get(key, []))


def remember(key, user_text, assistant_text):
    if not key:
        return
    with _lock:
        if key not in _conversations and len(_conversations) >= MAX_CONVERSATIONS:
            _conversations.pop(next(iter(_conversations)))
        turns = _conversations.setdefault(key, [])
        turns.append(("user", user_text))
        turns.append(("assistant", assistant_text))
        del turns[: -2 * MAX_TURNS]
