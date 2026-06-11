from sentinel import audit, rbac, store


def test_resolve_ticket_is_parameterized_and_closes(tmp_db):
    store.resolve_ticket("1")
    with store.db() as conn:
        status = conn.execute("SELECT status FROM tickets WHERE id=1").fetchone()[0]
    assert status == "closed"


def test_create_ticket(tmp_db):
    ticket_id = store.create_ticket("New thing", "bob")
    with store.db() as conn:
        row = conn.execute(
            "SELECT title, status, assignee FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
    assert (row["title"], row["status"], row["assignee"]) == ("New thing", "open", "bob")


def test_pending_action_lifecycle(tmp_db):
    pid = store.create_pending("U123", "C1", "write_query", {"query": "UPDATE x"})
    pending = store.get_pending(pid)
    assert pending["status"] == "pending"
    assert pending["requested_by"] == "U123"

    assert store.decide_pending(pid, "approved", "U999")
    assert store.get_pending(pid)["status"] == "approved"
    assert store.get_pending(pid)["decided_by"] == "U999"

    # second decision attempt loses the race
    assert not store.decide_pending(pid, "denied", "U777")
    assert store.get_pending(pid)["status"] == "approved"


def test_env_admin_is_admin(tmp_db, monkeypatch):
    monkeypatch.setenv("ADMIN_USERS", "U_ADMIN, U_OTHER")
    assert rbac.is_admin("U_ADMIN")
    assert rbac.get_role("U_STRANGER") == rbac.MEMBER


def test_db_role_is_admin(tmp_db, monkeypatch):
    monkeypatch.delenv("ADMIN_USERS", raising=False)
    store.set_role("U_DB_ADMIN", rbac.ADMIN)
    assert rbac.is_admin("U_DB_ADMIN")
    assert not rbac.is_admin("U_NOBODY")


def test_audit_record_and_recent(tmp_db):
    audit.record(
        actor="U1", provider="claude", tool="read_query",
        query="SELECT 1", decision="allowed", latency_ms=12,
    )
    audit.record(
        actor="U1", provider="claude", tool="write_query",
        query="UPDATE x", decision="queued", detail="approval #1",
    )
    rows = audit.recent(limit=5)
    assert len(rows) == 2
    assert rows[0]["decision"] == "queued"  # newest first
    assert rows[1]["decision"] == "allowed"
