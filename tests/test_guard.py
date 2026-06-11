from sentinel import guard


def test_plain_select_is_allowed():
    ok, _ = guard.validate_read_query("SELECT id, title FROM tickets ORDER BY id")
    assert ok


def test_lowercase_select_with_like_literal_is_allowed():
    ok, _ = guard.validate_read_query(
        "select id from tickets where lower(title) like '%error%'"
    )
    assert ok


def test_cte_select_is_allowed():
    ok, _ = guard.validate_read_query("WITH t AS (SELECT 1 AS n) SELECT n FROM t")
    assert ok


def test_trailing_semicolon_is_allowed():
    ok, _ = guard.validate_read_query("SELECT 1;")
    assert ok


def test_forbidden_word_inside_string_literal_is_allowed():
    ok, _ = guard.validate_read_query(
        "SELECT * FROM tickets WHERE title = 'please DROP everything and UPDATE me'"
    )
    assert ok


def test_delete_is_blocked():
    ok, reason = guard.validate_read_query("DELETE FROM tickets")
    assert not ok


def test_update_is_blocked():
    ok, _ = guard.validate_read_query("UPDATE tickets SET status='closed'")
    assert not ok


def test_multi_statement_is_blocked():
    ok, _ = guard.validate_read_query("SELECT 1; DROP TABLE tickets")
    assert not ok


def test_pragma_is_blocked():
    ok, _ = guard.validate_read_query("PRAGMA table_info(tickets)")
    assert not ok


def test_attach_is_blocked():
    ok, _ = guard.validate_read_query("ATTACH DATABASE 'x.db' AS x")
    assert not ok


def test_line_comment_is_blocked():
    ok, _ = guard.validate_read_query("SELECT * FROM tickets -- sneaky")
    assert not ok


def test_block_comment_is_blocked():
    ok, _ = guard.validate_read_query("SELECT/**/1")
    assert not ok


def test_select_hiding_delete_in_subquery_is_blocked():
    ok, _ = guard.validate_read_query("WITH t AS (DELETE FROM tickets) SELECT 1")
    assert not ok


def test_empty_query_is_blocked():
    ok, _ = guard.validate_read_query("")
    assert not ok


def test_non_select_first_keyword_is_blocked():
    ok, _ = guard.validate_read_query("VACUUM")
    assert not ok


# --- evaluate(): the policy layer ----------------------------------------------

def test_valid_read_query_is_allowed():
    decision, _ = guard.evaluate("read_query", {"query": "SELECT * FROM tickets"})
    assert decision == guard.ALLOW


def test_invalid_read_query_is_blocked():
    decision, _ = guard.evaluate("read_query", {"query": "DELETE FROM tickets"})
    assert decision == guard.BLOCK


def test_list_tables_is_allowed():
    decision, _ = guard.evaluate("list_tables", {})
    assert decision == guard.ALLOW


def test_describe_table_is_allowed():
    decision, _ = guard.evaluate("describe_table", {"table_name": "tickets"})
    assert decision == guard.ALLOW


def test_write_query_is_queued_for_approval():
    decision, _ = guard.evaluate(
        "write_query", {"query": "UPDATE tickets SET status='closed' WHERE id=1"}
    )
    assert decision == guard.QUEUE


def test_create_table_is_queued_for_approval():
    decision, _ = guard.evaluate("create_table", {"query": "CREATE TABLE x (id INT)"})
    assert decision == guard.QUEUE


def test_unknown_tool_is_blocked_by_default():
    decision, _ = guard.evaluate("shell_exec", {"cmd": "rm -rf /"})
    assert decision == guard.BLOCK
