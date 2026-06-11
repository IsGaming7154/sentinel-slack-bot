import logging

from sentinel import rbac
from sentinel.store import get_tickets, resolve_ticket

logger = logging.getLogger(__name__)

STATUS_EMOJI = {"open": "🔴", "in_progress": "🟡", "closed": "✅"}
ACTIVE_STATUSES = {"open", "in_progress"}
ROLE_BADGE = {rbac.ADMIN: ":shield: Admin", rbac.MEMBER: ":bust_in_silhouette: Member"}


def _ticket_card(ticket, is_admin):
    status = (ticket.get("status") or "").lower()
    emoji = STATUS_EMOJI.get(status, "⚪")
    title = ticket.get("title", "(untitled)")
    section = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "{} *{}*\n_ID {} • {} • {}_".format(
                emoji, title, ticket.get("id"), ticket.get("assignee", "?"), status
            ),
        },
    }
    if status in ACTIVE_STATUSES and is_admin:
        section["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Mark Resolved", "emoji": True},
            "style": "primary",
            "action_id": "mark_resolved",
            "value": str(ticket.get("id")),
        }
    return section


def build_home_view(tickets, role=rbac.MEMBER):
    is_admin = role == rbac.ADMIN
    active = sum(
        1 for t in tickets if (t.get("status") or "").lower() in ACTIVE_STATUSES
    )
    closed = len(tickets) - active

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Enterprise Agentic Control Panel",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Your role: {}".format(ROLE_BADGE.get(role, role)),
                }
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*Active:*\n{}".format(active)},
                {"type": "mrkdwn", "text": "*Closed:*\n{}".format(closed)},
            ],
        },
        {"type": "divider"},
    ]

    if not tickets:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_No tickets found._"},
            }
        )
    else:
        for ticket in tickets:
            blocks.append(_ticket_card(ticket, is_admin))
            blocks.append({"type": "divider"})

    return {"type": "home", "blocks": blocks}


def _error_view(message):
    return {
        "type": "home",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":warning: {}".format(message)},
            }
        ],
    }


def register(app):
    def _publish(client, user_id):
        client.views_publish(
            user_id=user_id,
            view=build_home_view(get_tickets(), rbac.get_role(user_id)),
        )

    @app.event("app_home_opened")
    def handle_home_opened(event, client):
        user_id = event["user"]
        try:
            _publish(client, user_id)
        except Exception:
            logger.exception("Failed to render App Home for %s.", user_id)
            client.views_publish(
                user_id=user_id, view=_error_view("Could not load tickets right now.")
            )

    @app.action("mark_resolved")
    def handle_mark_resolved(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        try:
            # Buttons are hidden from non-admins, but enforce server-side too.
            if rbac.is_admin(user_id):
                resolve_ticket(body["actions"][0]["value"])
            _publish(client, user_id)
        except Exception:
            logger.exception("Failed to resolve ticket for %s.", user_id)
            client.views_publish(
                user_id=user_id, view=_error_view("Could not update the ticket.")
            )
