import asyncio
import logging
import os

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from sentinel import config, mcp_bridge, monitor, store
from sentinel.handlers import approvals, home, messages

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)

messages.register(app)
home.register(app)
approvals.register(app)


if __name__ == "__main__":
    store.ensure_schema()
    mcp_bridge.start()
    if config.INCIDENT_MONITOR:
        asyncio.run_coroutine_threadsafe(monitor.incident_monitor(app), mcp_bridge.loop)
    else:
        logger.info("Incident monitor disabled (set INCIDENT_MONITOR=1 to enable).")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
