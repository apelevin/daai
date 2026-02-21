import json
import logging
import os
import ssl
import time
import asyncio

# Monkey-patch: fix mattermostdriver bug (uses ssl.Purpose.CLIENT_AUTH instead of
# ssl.Purpose.SERVER_AUTH for WebSocket client connections). Must be applied before
# mattermostdriver.websocket is first used.
_original_create_default_context = ssl.create_default_context


def _patched_create_default_context(purpose=ssl.Purpose.SERVER_AUTH, **kw):
    if purpose == ssl.Purpose.CLIENT_AUTH:
        purpose = ssl.Purpose.SERVER_AUTH
    return _original_create_default_context(purpose=purpose, **kw)


ssl.create_default_context = _patched_create_default_context

from mattermostdriver import Driver  # noqa: E402

logger = logging.getLogger(__name__)


class MattermostClient:
    def __init__(self):
        url = os.environ["MATTERMOST_URL"]
        # Strip protocol and trailing slash for mattermostdriver
        host = url.replace("https://", "").replace("http://", "").rstrip("/")
        scheme = "https" if url.startswith("https") else "http"

        self.channel_id = os.environ.get("DATA_CONTRACTS_CHANNEL_ID", "")

        # Support both token and login/password auth
        token = os.environ.get("MATTERMOST_BOT_TOKEN", "").strip()
        login = os.environ.get("MATTERMOST_LOGIN", "").strip()
        password = os.environ.get("MATTERMOST_PASSWORD", "").strip()

        driver_opts = {
            "url": host,
            "scheme": scheme,
            "port": 443 if scheme == "https" else 8065,
            "verify": True,
        }

        if token and token != "your_bot_token_here":
            driver_opts["token"] = token
        elif login and password:
            driver_opts["login_id"] = login
            driver_opts["password"] = password
        else:
            raise ValueError(
                "Set either MATTERMOST_BOT_TOKEN or MATTERMOST_LOGIN + MATTERMOST_PASSWORD"
            )

        self.driver = Driver(driver_opts)
        self.driver.login()

        # Resolve own user id
        me = self.driver.users.get_user("me")
        self.bot_user_id = me["id"]
        self._username = me["username"]
        logger.info("Mattermost: logged in as @%s (id=%s)", self._username, self.bot_user_id)

    # ── Sending messages ────────────────────────────────────────────

    def send_to_channel(self, message: str, root_id: str | None = None) -> dict:
        """Send a message to Data Contracts channel. If root_id — reply in thread."""
        post = {
            "channel_id": self.channel_id,
            "message": message,
        }
        if root_id:
            post["root_id"] = root_id
        resp = self.driver.posts.create_post(post)
        logger.debug("Sent to channel, post_id=%s", resp["id"])
        return resp

    def send_dm(self, user_id: str, message: str) -> dict:
        """Send a direct message to a user."""
        dm_channel = self.driver.channels.create_direct_message_channel(
            [self.bot_user_id, user_id]
        )
        post = {
            "channel_id": dm_channel["id"],
            "message": message,
        }
        resp = self.driver.posts.create_post(post)
        logger.debug("Sent DM to %s, post_id=%s", user_id, resp["id"])
        return resp

    def send_to_channel_id(self, channel_id: str, message: str, root_id: str | None = None) -> dict:
        """Send a message to an arbitrary channel."""
        post = {
            "channel_id": channel_id,
            "message": message,
        }
        if root_id:
            post["root_id"] = root_id
        return self.driver.posts.create_post(post)

    # ── Reading data ────────────────────────────────────────────────

    def get_user_info(self, user_id: str) -> dict:
        """Get username and display name for a user."""
        user = self.driver.users.get_user(user_id)
        return {
            "user_id": user["id"],
            "username": user["username"],
            "display_name": f'{user.get("first_name", "")} {user.get("last_name", "")}'.strip(),
            "email": user.get("email", ""),
        }

    def get_channel_members(self) -> list[dict]:
        """Get members of the Data Contracts channel."""
        members = self.driver.channels.get_channel_members(self.channel_id)
        result = []
        for m in members:
            try:
                info = self.get_user_info(m["user_id"])
                result.append(info)
            except Exception as e:
                logger.warning("Failed to get user info for %s: %s", m["user_id"], e)
        return result

    def get_thread(self, post_id: str) -> list[dict]:
        """Get all messages in a thread, ordered chronologically."""
        thread = self.driver.posts.get_thread(post_id)
        posts = thread.get("posts", {})
        order = thread.get("order", [])
        result = []
        for pid in order:
            p = posts.get(pid, {})
            result.append({
                "id": p.get("id"),
                "user_id": p.get("user_id"),
                "message": p.get("message", ""),
                "create_at": p.get("create_at", 0),
            })
        return result

    def get_channel_info(self, channel_id: str) -> dict:
        """Get channel details."""
        return self.driver.channels.get_channel(channel_id)

    # ── Discovery helpers ───────────────────────────────────────────

    def get_teams(self) -> list[dict]:
        """Get all teams the user belongs to."""
        return self.driver.teams.get_user_teams(self.bot_user_id)

    def get_channels_for_team(self, team_id: str) -> list[dict]:
        """Get all channels in a team that the user is a member of."""
        return self.driver.channels.get_channels_for_user(self.bot_user_id, team_id)

    # ── WebSocket ───────────────────────────────────────────────────

    def connect_websocket(self, callback):
        """Connect to Mattermost WebSocket with auto-reconnect.

        mattermostdriver's websocket client relies on asyncio event loop.
        When running in a plain thread, we need to ensure an event loop exists.
        """
        backoff = 2
        max_backoff = 60

        while True:
            try:
                logger.info("Connecting to Mattermost WebSocket...")

                # Ensure there's an asyncio event loop in this thread.
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                self.driver.init_websocket(callback)
            except Exception as e:
                logger.error("WebSocket error: %s", e)

            logger.warning("WebSocket disconnected, reconnecting in %ds...", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
