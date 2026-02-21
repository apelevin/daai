import time
import uuid
from dataclasses import dataclass


@dataclass
class FakeUser:
    user_id: str
    username: str
    display_name: str = ""


class FakeMattermostClient:
    """A minimal in-process simulation of MattermostClient.

    - Stores posts and DMs in memory
    - Supports threads via root_id
    - Provides get_thread() and get_user_info()

    This is intentionally tiny: it exists to test Agent/Listener logic
    without any external integrations.
    """

    def __init__(self, *, channel_id: str = "data-contracts", bot_user_id: str = "bot"):
        self.channel_id = channel_id
        self.bot_user_id = bot_user_id
        self._users: dict[str, FakeUser] = {}
        self._posts: dict[str, dict] = {}
        self._order: list[str] = []
        self._dm_channels: dict[tuple[str, str], str] = {}

        # register bot user
        self.register_user(user_id=bot_user_id, username="ai_architect", display_name="AI-архитектор")

    # ── Users ───────────────────────────────────────────────────

    def register_user(self, *, user_id: str, username: str, display_name: str = "") -> None:
        self._users[user_id] = FakeUser(user_id=user_id, username=username, display_name=display_name)

    def get_user_info(self, user_id: str) -> dict:
        u = self._users[user_id]
        return {
            "user_id": u.user_id,
            "username": u.username,
            "display_name": u.display_name,
            "email": "",
        }

    # ── Posting ────────────────────────────────────────────────

    def _new_post_id(self) -> str:
        return uuid.uuid4().hex

    def send_to_channel(self, message: str, root_id: str | None = None) -> dict:
        return self._record_post(
            user_id=self.bot_user_id,
            channel_id=self.channel_id,
            message=message,
            root_id=root_id or "",
        )

    def send_dm(self, user_id: str, message: str) -> dict:
        # create synthetic dm channel id
        key = tuple(sorted((self.bot_user_id, user_id)))
        dm_channel_id = self._dm_channels.get(key)
        if not dm_channel_id:
            dm_channel_id = f"dm-{key[0]}-{key[1]}"
            self._dm_channels[key] = dm_channel_id

        return self._record_post(
            user_id=self.bot_user_id,
            channel_id=dm_channel_id,
            message=message,
            root_id="",
        )

    def _record_post(self, *, user_id: str, channel_id: str, message: str, root_id: str = "") -> dict:
        post_id = self._new_post_id()
        post = {
            "id": post_id,
            "user_id": user_id,
            "channel_id": channel_id,
            "message": message,
            "root_id": root_id,
            "create_at": int(time.time() * 1000),
        }
        self._posts[post_id] = post
        self._order.append(post_id)
        return post

    def record_user_post(self, *, user_id: str, channel_id: str, message: str, root_id: str = "") -> dict:
        """Record an inbound (human) post so transcripts include both sides."""
        return self._record_post(user_id=user_id, channel_id=channel_id, message=message, root_id=root_id)

    def get_thread(self, post_id: str) -> list[dict]:
        # Return posts where id==post_id or root_id==post_id, in chronological order.
        items = []
        for pid in self._order:
            p = self._posts[pid]
            if p["id"] == post_id or p.get("root_id") == post_id:
                items.append({
                    "id": p["id"],
                    "user_id": p["user_id"],
                    "message": p["message"],
                    "create_at": p["create_at"],
                })
        return items

    # ── Helpers for tests ──────────────────────────────────────

    def list_channel_thread(self, root_id: str) -> list[dict]:
        return self.get_thread(root_id)

    def all_posts(self) -> list[dict]:
        return [self._posts[pid] for pid in self._order]
