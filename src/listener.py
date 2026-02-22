import json
import logging
import threading

logger = logging.getLogger(__name__)


class Listener:
    def __init__(self, agent, mattermost_client):
        self.agent = agent
        self.mm = mattermost_client
        # De-duplication: Mattermost WS can occasionally deliver duplicate 'posted' events.
        # Also guard against concurrent callback invocations.
        self._seen_post_ids = set()
        self._inflight_post_ids = set()
        self._dedup_lock = threading.Lock()

    def start(self):
        """Connect to WebSocket and start processing events."""
        logger.info("Starting Mattermost listener...")
        self.mm.connect_websocket(self._handle_event_async)

    async def _handle_event_async(self, event_raw):
        """Async wrapper required by mattermostdriver WebSocket."""
        self._handle_event(event_raw)

    def _handle_event(self, event_raw):
        """Handle a raw WebSocket event."""
        try:
            if isinstance(event_raw, str):
                event = json.loads(event_raw)
            else:
                event = event_raw

            event_type = event.get("event")
            if not event_type:
                return

            if event_type == "posted":
                self._handle_posted(event)
            elif event_type == "user_added":
                self._handle_user_added(event)
            elif event_type in ("user_removed", "user_removed_from_channel"):
                self._handle_user_removed(event)
        except Exception as e:
            logger.error("Error handling event: %s", e, exc_info=True)

    def _handle_posted(self, event):
        """Handle a new message event."""
        data = event.get("data", {})
        post_raw = data.get("post")
        if not post_raw:
            return

        if isinstance(post_raw, str):
            post = json.loads(post_raw)
        else:
            post = post_raw

        # Ignore bot's own messages
        user_id = post.get("user_id", "")
        if user_id == self.mm.bot_user_id:
            return

        post_type = post.get("type", "") or ""

        channel_id = post.get("channel_id", "")
        message = post.get("message", "").strip()

        # Handle system "added/removed to channel" posts (often batched) as membership events.
        if channel_id == self.mm.channel_id and post_type in ("system_add_to_channel", "system_remove_from_channel"):
            try:
                self._handle_system_membership_post(post_type, message)
            except Exception as e:
                logger.error("Failed to handle system membership post: %s", e, exc_info=True)
            return
        post_id = post.get("id", "")
        root_id = post.get("root_id", "")

        if not message:
            return

        # De-dup posted events (and prevent concurrent double-processing)
        if post_id:
            with self._dedup_lock:
                if post_id in self._seen_post_ids or post_id in self._inflight_post_ids:
                    return
                self._inflight_post_ids.add(post_id)

        # Get username
        try:
            user_info = self.mm.get_user_info(user_id)
            username = user_info["username"]
        except Exception as e:
            logger.error("Failed to get user info for %s: %s", user_id, e)
            username = "unknown"

        # Determine channel type
        channel_type_raw = data.get("channel_type", "")
        if channel_type_raw == "D":
            channel_type = "dm"
        elif channel_id == self.mm.channel_id:
            channel_type = "channel"
        else:
            # Message in some other channel — ignore
            return

        # Get thread context if this is a reply
        thread_context = None
        if root_id:
            try:
                thread_posts = self.mm.get_thread(root_id)
                # Build context from thread (exclude current message)
                context_parts = []
                for tp in thread_posts:
                    if tp["id"] == post_id:
                        continue
                    tp_user_id = tp.get("user_id", "")
                    if tp_user_id == self.mm.bot_user_id:
                        tp_name = "AI-архитектор"
                    else:
                        try:
                            tp_info = self.mm.get_user_info(tp_user_id)
                            tp_name = f"@{tp_info['username']}"
                        except Exception:
                            tp_name = "unknown"
                    context_parts.append(f"{tp_name}: {tp['message']}")
                thread_context = "\n".join(context_parts) if context_parts else None
            except Exception as e:
                logger.warning("Failed to get thread context for %s: %s", root_id, e)

        # If participant profile is missing, start onboarding in DM.
        # (user_added event doesn't fire for users who were already in channel before bot started.)
        if channel_type == "channel":
            try:
                existing_profile = self.agent.memory.get_participant(username)
            except Exception:
                existing_profile = None

            already_onboarded = False
            try:
                already_onboarded = self.agent.memory.is_participant_onboarded(username)
            except Exception:
                already_onboarded = False

            if (not existing_profile) and (not already_onboarded):
                try:
                    user_info = self.mm.get_user_info(user_id)
                    self.agent.onboard_participant(
                        user_id=user_id,
                        username=user_info["username"],
                        display_name=user_info["display_name"],
                    )
                    # Let them know in-thread that a DM is waiting.
                    thread_root = root_id or post_id
                    self.mm.send_to_channel(
                        f"@{username}, я написал(а) тебе в личку 3 коротких вопроса для профиля. Ответь там — и продолжим.",
                        root_id=thread_root,
                    )

                    # If this looks like a simple hello/first ping (not an actual request),
                    # stop here to avoid spamming the channel with a second long welcome.
                    low = message.lower()
                    looks_like_real_request = (
                        ("?" in message)
                        or any(k in low for k in [
                            "контракт", "статус", "начни", "покажи", "очеред", "план", "расхожд", "проблем",
                            "сохрани", "сохран", "зафикс", "обнов", "создай", "создать",
                            "аудит", "конфликт", "проверь",
                            "reminder", "дайджест", "digest",
                        ])
                    )
                    if not looks_like_real_request and len(message) <= 120:
                        return

                except Exception as e:
                    logger.error("Failed to onboard participant on first message: %s", e, exc_info=True)

        # Process message
        logger.info(
            "Processing message post_id=%s root_id=%s from @%s in %s: %s",
            post_id,
            root_id,
            username,
            channel_type,
            message[:100],
        )
        try:
            reply = self.agent.process_message(
                username=username,
                message=message,
                channel_type=channel_type,
                thread_context=thread_context,
                post_id=post_id,
            )
        except Exception as e:
            logger.error("Agent failed to process message: %s", e, exc_info=True)
            reply = "Произошла ошибка при обработке сообщения. Попробуй ещё раз."

        if not reply:
            if post_id:
                with self._dedup_lock:
                    self._inflight_post_ids.discard(post_id)
                    self._seen_post_ids.add(post_id)
            return

        # Send reply
        try:
            if channel_type == "dm":
                logger.info("Sending DM reply to user_id=%s (post_id=%s) len=%s", user_id, post_id, len(reply))
                self.mm.send_dm(user_id, reply)
            else:
                # Reply in thread (use root_id if exists, otherwise start thread from this post)
                thread_root = root_id or post_id
                logger.info(
                    "Sending channel reply root=%s (inbound post_id=%s) len=%s preview=%r",
                    thread_root,
                    post_id,
                    len(reply),
                    reply[:80],
                )
                self.mm.send_to_channel(reply, root_id=thread_root)
        except Exception as e:
            logger.error("Failed to send reply: %s", e, exc_info=True)
        finally:
            # Mark this post id as processed
            if post_id:
                with self._dedup_lock:
                    self._inflight_post_ids.discard(post_id)
                    self._seen_post_ids.add(post_id)
                    # prevent unbounded growth
                    if len(self._seen_post_ids) > 4000:
                        self._seen_post_ids = set(list(self._seen_post_ids)[-2000:])

    def _handle_user_removed(self, event):
        """Handle a user leaving/removal from the Data Contracts channel."""
        data = event.get("data", {})
        channel_id = event.get("broadcast", {}).get("channel_id", "")
        if channel_id != self.mm.channel_id:
            return

        user_id = data.get("user_id", "")
        if not user_id or user_id == self.mm.bot_user_id:
            return

        try:
            user_info = self.mm.get_user_info(user_id)
            try:
                self.agent.memory.set_participant_active(user_info["username"], False)
            except Exception:
                pass
            # Best-effort: announce removal (optional). Keep it quiet for now.
            logger.info("Participant removed from channel: @%s", user_info["username"])
        except Exception as e:
            logger.error("Failed to handle user_removed for %s: %s", user_id, e, exc_info=True)

    def _handle_system_membership_post(self, post_type: str, message: str) -> None:
        """Handle Mattermost system posts like "@A and 3 others added".

        Some Mattermost setups may not emit user_added/user_removed WS events reliably,
        but these system posts always appear as a normal 'posted' event.
        We parse mentioned @usernames and onboard/mark inactive.
        """
        # Extract @mentions (best-effort). Mattermost usernames are usually [a-z0-9._-]
        import re
        usernames = re.findall(r"@([a-zA-Z0-9._-]+)", message)
        if not usernames:
            return

        if post_type == "system_add_to_channel":
            for uname in usernames:
                try:
                    # resolve user id by username
                    u = self.mm.driver.users.get_user_by_username(uname)
                    user_id = u.get("id")
                    display_name = f"{u.get('first_name','')} {u.get('last_name','')}".strip() or uname
                    # mark active and onboard (idempotent)
                    try:
                        self.agent.memory.set_participant_active(uname, True)
                    except Exception:
                        pass
                    self.agent.onboard_participant(user_id=user_id, username=uname, display_name=display_name)
                except Exception:
                    # If we can't resolve, skip.
                    continue

            # Public welcome (single message, mentions those we saw)
            try:
                mentions = " ".join([f"@{u}" for u in usernames[:8]])
                more = "" if len(usernames) <= 8 else f" и ещё {len(usernames) - 8}"
                self.mm.send_to_channel(
                    f"Добро пожаловать, {mentions}{more}! Я написал(а) вам в личку 3 коротких вопроса для онбординга.",
                    root_id=None,
                )
            except Exception:
                pass

        elif post_type == "system_remove_from_channel":
            for uname in usernames:
                try:
                    self.agent.memory.set_participant_active(uname, False)
                except Exception:
                    pass
            logger.info("System removal processed for: %s", ",".join(usernames))

    def _handle_user_added(self, event):
        """Handle a new user joining the Data Contracts channel."""
        data = event.get("data", {})
        # Only care about the Data Contracts channel
        channel_id = event.get("broadcast", {}).get("channel_id", "")
        if channel_id != self.mm.channel_id:
            return

        user_id = data.get("user_id", "")
        if not user_id or user_id == self.mm.bot_user_id:
            return

        try:
            user_info = self.mm.get_user_info(user_id)
            self.agent.onboard_participant(
                user_id=user_id,
                username=user_info["username"],
                display_name=user_info["display_name"],
            )
            # Public welcome in channel + DM onboarding
            try:
                self.mm.send_to_channel(
                    f"Добро пожаловать, @{user_info['username']}! Я написал(а) тебе в личку 3 коротких вопроса для онбординга.",
                    root_id=None,
                )
            except Exception:
                pass
        except Exception as e:
            logger.error("Failed to onboard user %s: %s", user_id, e, exc_info=True)
