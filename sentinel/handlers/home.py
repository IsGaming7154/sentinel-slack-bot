import logging

from sentinel.store import get_tickets, resolve_ticket

logger = logging.getLogger(__name__)

STATUS_EMOJI = {"open": "🔴", "in_progress": "🟡", "closed": "✅"}
ACTIVE_STATUSES = {"open", "in_progress"}


def _ticket_card(ticket):
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
    if status in ACTIVE_STATUSES:
        section["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Mark Resolved", "emoji": True},
            "style": "primary",
            "action_id": "mark_resolved",
            "value": str(ticket.get("id")),
        }
    return section


def build_home_view(tickets):
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
            blocks.append(_ticket_card(ticket))
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
    @app.event("app_home_opened")
    def handle_home_opened(event, client):
        user_id = event["user"]
        try:
            tickets = get_tickets()
            client.views_publish(user_id=user_id, view=build_home_view(tickets))
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
            ticket_id = body["actions"][0]["value"]
            resolve_ticket(ticket_id)
            tickets = get_tickets()
            client.views_publish(user_id=user_id, view=build_home_view(tickets))
        except Exception:
            logger.exception("Failed to resolve ticket for %s.", user_id)
            client.views_publish(
                user_id=user_id, view=_error_view("Could not update the ticket.")
            )
