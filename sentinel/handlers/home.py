import logging

from sentinel import audit, mcp_bridge, rbac
from sentinel.llm import router
from sentinel.store import create_ticket, get_tickets, resolve_ticket

logger = logging.getLogger(__name__)

STATUS_EMOJI = {"open": "🔴", "in_progress": "🟡", "closed": "✅"}
ACTIVE_STATUSES = {"open", "in_progress"}
ROLE_BADGE = {rbac.ADMIN: ":shield: Admin", rbac.MEMBER: ":bust_in_silhouette: Member"}

FILTERS = ["all", "open", "in_progress", "closed"]
FILTER_LABEL = {
    "all": "All tickets",
    "open": "🔴 Open",
    "in_progress": "🟡 In progress",
    "closed": "✅ Closed",
}
DECISION_EMOJI = {
    "allowed": "✅",
    "queued": "⏳",
    "approved": "👍",
    "denied": "🚫",
    "blocked": "⛔",
    "unauthorized": "🚨",
}

HEALTH_EMOJI = {
    "operational": "✅",
    "degraded": "🟡",
    "probing": "🟡",
    "down": "⛔",
}
ROUTER_STATE_LABEL = {
    router.CLOSED: "operational",
    router.DEGRADED: "degraded",
    router.HALF_OPEN: "probing",
    router.OPEN: "down",
}

# Per-user Home filter selection; purely cosmetic state, fine to lose on restart.
_user_filters = {}


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


def _controls(status_filter, is_admin):
    options = [
        {
            "text": {"type": "plain_text", "text": FILTER_LABEL[f], "emoji": True},
            "value": f,
        }
        for f in FILTERS
    ]
    current = next(o for o in options if o["value"] == status_filter)
    elements = [
        {
            "type": "static_select",
            "action_id": "filter_tickets",
            "initial_option": current,
            "options": options,
        }
    ]
    if is_admin:
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "➕ New Ticket", "emoji": True},
                "style": "primary",
                "action_id": "new_ticket_open",
            }
        )
    return {"type": "actions", "block_id": "home_controls", "elements": elements}


def _health_blocks(health):
    if not health:
        return []
    line = "   ".join(
        "{} *{}:* {}".format(HEALTH_EMOJI.get(state, "•"), component, state)
        for component, state in health.items()
    )
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":heartpulse: *System health*"},
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": line}]},
    ]


def _activity_blocks(activity):
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":shield: *Recent guard activity*"},
        }
    ]
    if not activity:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "_No guarded tool calls yet._"}],
            }
        )
        return blocks
    lines = []
    for row in activity:
        emoji = DECISION_EMOJI.get(row.get("decision"), "•")
        query = (row.get("query") or "").replace("\n", " ")[:80]
        lines.append(
            "{} *{}* `{}` · {} · {}".format(
                emoji,
                row.get("decision"),
                row.get("tool"),
                row.get("provider") or "?",
                query,
            )
        )
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(lines)}]}
    )
    return blocks


def build_home_view(
    tickets, role=rbac.MEMBER, status_filter="all", activity=None, health=None
):
    is_admin = role == rbac.ADMIN
    if status_filter not in FILTERS:
        status_filter = "all"
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
        _controls(status_filter, is_admin),
        {"type": "divider"},
    ]

    if status_filter == "all":
        shown = tickets
    else:
        shown = [
            t for t in tickets if (t.get("status") or "").lower() == status_filter
        ]

    if not shown:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No tickets match this filter._",
                },
            }
        )
        blocks.append({"type": "divider"})
    else:
        for ticket in shown:
            blocks.append(_ticket_card(ticket, is_admin))
            blocks.append({"type": "divider"})

    blocks.extend(_health_blocks(health))
    blocks.extend(_activity_blocks(activity or []))
    return {"type": "home", "blocks": blocks}


def _system_health():
    states = router.health()
    return {
        "Claude": ROUTER_STATE_LABEL.get(states["claude"], states["claude"]),
        "Gemini": ROUTER_STATE_LABEL.get(states["gemini"], states["gemini"]),
        "MCP": "operational" if mcp_bridge.client else "down",
    }


def _new_ticket_modal():
    status_options = [
        {"text": {"type": "plain_text", "text": "Open"}, "value": "open"},
        {"text": {"type": "plain_text", "text": "In progress"}, "value": "in_progress"},
    ]
    return {
        "type": "modal",
        "callback_id": "new_ticket_submit",
        "title": {"type": "plain_text", "text": "New Ticket"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "title",
                "label": {"type": "plain_text", "text": "Title"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "v",
                    "max_length": 150,
                },
            },
            {
                "type": "input",
                "block_id": "assignee",
                "label": {"type": "plain_text", "text": "Assignee"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "v",
                    "max_length": 50,
                },
            },
            {
                "type": "input",
                "block_id": "status",
                "label": {"type": "plain_text", "text": "Status"},
                "element": {
                    "type": "static_select",
                    "action_id": "v",
                    "initial_option": status_options[0],
                    "options": status_options,
                },
            },
        ],
    }


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
            view=build_home_view(
                get_tickets(),
                rbac.get_role(user_id),
                _user_filters.get(user_id, "all"),
                audit.recent(limit=5),
                _system_health(),
            ),
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

    @app.action("filter_tickets")
    def handle_filter(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        selected = body["actions"][0]["selected_option"]["value"]
        _user_filters[user_id] = selected if selected in FILTERS else "all"
        try:
            _publish(client, user_id)
        except Exception:
            logger.exception("Failed to re-render App Home for %s.", user_id)

    @app.action("new_ticket_open")
    def handle_new_ticket_open(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        # Button is admin-only in the view, but enforce server-side too.
        if not rbac.is_admin(user_id):
            return
        try:
            client.views_open(trigger_id=body["trigger_id"], view=_new_ticket_modal())
        except Exception:
            logger.exception("Failed to open new-ticket modal for %s.", user_id)

    @app.view("new_ticket_submit")
    def handle_new_ticket_submit(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        if not rbac.is_admin(user_id):
            audit.record(
                actor=user_id,
                provider="ui",
                tool="create_ticket",
                query="",
                decision="unauthorized",
                detail="non-admin tried to create a ticket",
            )
            return
        values = body["view"]["state"]["values"]
        title = (values["title"]["v"]["value"] or "").strip()
        assignee = (values["assignee"]["v"]["value"] or "").strip()
        status = values["status"]["v"]["selected_option"]["value"]
        try:
            ticket_id = create_ticket(title, assignee, status)
            audit.record(
                actor=user_id,
                provider="ui",
                tool="create_ticket",
                query="{} → {}".format(title, assignee),
                decision="allowed",
                detail="ticket #{} via App Home modal".format(ticket_id),
            )
            _publish(client, user_id)
        except Exception:
            logger.exception("Failed to create ticket for %s.", user_id)
