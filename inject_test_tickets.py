import sqlite3

DB_PATH = "data.db"

TEST_TICKETS = [
    ("API gateway timeout error on /checkout", "open", "dave"),
    ("Database connection timeout error", "open", "erin"),
    ("Payment service request timeout error", "open", "frank"),
]


def inject():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO tickets (title, status, assignee) VALUES (?, ?, ?)",
        TEST_TICKETS,
    )
    conn.commit()
    count = cur.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    conn.close()
    print(f"Injected {len(TEST_TICKETS)} timeout-error tickets. Table now has {count} rows.")


if __name__ == "__main__":
    inject()
