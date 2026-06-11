from sentinel import rbac
from sentinel.handlers.home import build_home_view

TICKETS = [
    {"id": 1, "title": "Login broken", "status": "open", "assignee": "alice"},
    {"id": 2, "title": "Slow search", "status": "in_progress", "assignee": "bob"},
    {"id": 3, "title": "Old bug", "status": "closed", "assignee": "carol"},
]


def _action_ids(view):
    ids = []
    for block in view["blocks"]:
        for el in block.get("elements", []):
            if "action_id" in el:
                ids.append(el["action_id"])
        if "accessory" in block:
            ids.append(block["accessory"]["action_id"])
    return ids


def _text(view):
    return str(view["blocks"])


def test_admin_sees_new_ticket_and_resolve_buttons():
    view = build_home_view(TICKETS, role=rbac.ADMIN)
    ids = _action_ids(view)
    assert "new_ticket_open" in ids
    assert "mark_resolved" in ids
    assert "filter_tickets" in ids


def test_member_gets_no_write_controls():
    view = build_home_view(TICKETS, role=rbac.MEMBER)
    ids = _action_ids(view)
    assert "new_ticket_open" not in ids
    assert "mark_resolved" not in ids
    assert "filter_tickets" in ids


def test_status_filter_narrows_tickets():
    view = build_home_view(TICKETS, role=rbac.MEMBER, status_filter="closed")
    text = _text(view)
    assert "Old bug" in text
    assert "Login broken" not in text


def test_unknown_filter_falls_back_to_all():
    view = build_home_view(TICKETS, role=rbac.MEMBER, status_filter="nonsense")
    text = _text(view)
    assert "Login broken" in text and "Old bug" in text


def test_activity_feed_rendered():
    activity = [
        {
            "decision": "blocked",
            "tool": "write_query",
            "provider": "claude",
            "query": "DELETE FROM tickets",
        }
    ]
    view = build_home_view(TICKETS, role=rbac.MEMBER, activity=activity)
    text = _text(view)
    assert "Recent guard activity" in text
    assert "DELETE FROM tickets" in text


def test_empty_activity_shows_placeholder():
    view = build_home_view(TICKETS, role=rbac.MEMBER)
    assert "No guarded tool calls yet" in _text(view)
