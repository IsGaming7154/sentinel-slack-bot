import sqlite3

from sentinel.config import DB_PATH
from sentinel.store import ensure_schema

SAMPLE_TICKETS = [
    (1, "Login page returns 500 error", "open", "alice"),
    (2, "Add dark mode to settings", "in_progress", "bob"),
    (3, "Export reports to CSV", "closed", "carol"),
]


def setup():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS tickets")
    cur.execute(
        """
        CREATE TABLE tickets (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            assignee TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.executemany(
        "INSERT INTO tickets (id, title, status, assignee) VALUES (?, ?, ?, ?)",
        SAMPLE_TICKETS,
    )
    conn.commit()

    count = cur.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    conn.close()

    ensure_schema()
    print(f"Created {DB_PATH} with 'tickets' table ({count} rows) + Sentinel control tables.")


if __name__ == "__main__":
    setup()
