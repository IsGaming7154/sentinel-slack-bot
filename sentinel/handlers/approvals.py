"""Human-in-the-loop approval cards for write operations queued by the guard."""

import json
import logging

from sentinel import audit, mcp_bridge, rbac, store

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 600


def build_approval_blocks(pending_id, requested_by, tool, query):
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":shield: *Sentinel approval required*\n"
                    "The agent wants to run a *write operation* on behalf of "
                    "<@{}>.".format(requested_by)
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Tool:* `{}`\n```{}```".format(tool, query),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "approve_action",
                    "value": str(pending_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny", "emoji": True},
                    "style": "danger",
                    "action_id": "deny_action",
                    "value": str(pending_id),
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Request #{} • only admins can decide • nothing runs "
                    "until approved".format(pending_id),
                }
            ],
        },
    ]


def _settled_blocks(text):
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]


def _decide(body, client, approve):
    user_id = body["user"]["id"]
    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    pending_id = body["actions"][0]["value"]

    pending = store.get_pending(pending_id)
    if pending is None:
        client.chat_postEphemeral(
            channel=channel, user=user_id,
            text="Approval request #{} no longer exists.".format(pending_id),
        )
        return

    arguments = json.loads(pending["arguments"])
    query = arguments.get("query", pending["arguments"])

    if not rbac.is_admin(user_id):
        audit.record(
            actor=user_id, provider="human", tool=pending["tool"], query=query,
            decision="unauthorized",
            detail="non-admin tried to decide approval #{}".format(pending_id),
        )
        client.chat_postEphemeral(
            channel=channel, user=user_id,
            text=":shield: Only admins can decide approval requests. "
            "Your role: {}.".format(rbac.get_role(user_id)),
        )
        return

    status = "approved" if approve else "denied"
    if not store.decide_pending(pending_id, status, user_id):
        client.chat_postEphemeral(
            channel=channel, user=user_id,
            text="Approval request #{} was already decided.".format(pending_id),
        )
        return

    if approve:
        try:
            output = mcp_bridge.call_tool_sync(pending["tool"], arguments)
        except Exception as e:
            logger.exception("Approved action #%s failed to execute.", pending_id)
            output = "Execution failed: {}".format(e)
        audit.record(
            actor=user_id, provider="human", tool=pending["tool"], query=query,
            decision="approved", detail="approval #{}".format(pending_id),
        )
        if len(output) > MAX_RESULT_CHARS:
            output = output[:MAX_RESULT_CHARS] + "…"
        text = (
            ":white_check_mark: *Approved* by <@{}> — `{}` executed.\n"
            "```{}```\n*Result:* {}".format(user_id, pending["tool"], query, output)
        )
    else:
        audit.record(
            actor=user_id, provider="human", tool=pending["tool"], query=query,
            decision="denied", detail="approval #{}".format(pending_id),
        )
        text = (
            ":no_entry: *Denied* by <@{}>. Nothing was executed.\n"
            "```{}```".format(user_id, query)
        )

    client.chat_update(
        channel=channel, ts=message_ts, text=text, blocks=_settled_blocks(text)
    )


def register(app):
    @app.action("approve_action")
    def handle_approve(ack, body, client):
        ack()
        _decide(body, client, approve=True)

    @app.action("deny_action")
    def handle_deny(ack, body, client):
        ack()
        _decide(body, client, approve=False)
