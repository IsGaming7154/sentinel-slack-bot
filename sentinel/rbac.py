"""Role-based access control.

Admins come from the ADMIN_USERS env var (comma-separated Slack user IDs)
or the user_roles table. Everyone else is a member: they can ask and read,
but cannot approve writes, resolve tickets, or create tickets.
"""

import os

from sentinel import store

ADMIN = "admin"
MEMBER = "member"


def _env_admins():
    raw = os.environ.get("ADMIN_USERS", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


def get_role(user_id):
    if user_id in _env_admins():
        return ADMIN
    return store.get_role_row(user_id) or MEMBER


def is_admin(user_id):
    return get_role(user_id) == ADMIN
