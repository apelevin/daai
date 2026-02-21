"""Discover Mattermost IDs: teams, channels, own user ID.

Run this after filling MATTERMOST_URL + login/password in env.local.
It will print the values to put into env.local.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "env.local"))

from src.mattermost_client import MattermostClient


def main():
    print("=" * 60)
    print("Mattermost Discovery")
    print("=" * 60)

    mm = MattermostClient()

    print(f"\n✅ Logged in as: @{mm._username}")
    print(f"   MATTERMOST_BOT_USER_ID={mm.bot_user_id}")

    # Teams
    print("\n--- Teams ---")
    teams = mm.get_teams()
    for t in teams:
        print(f"  [{t['id']}] {t['display_name']} (name={t['name']})")

    if not teams:
        print("  No teams found!")
        return

    # Channels for each team
    for t in teams:
        print(f"\n--- Channels in '{t['display_name']}' ---")
        channels = mm.get_channels_for_team(t["id"])
        # Sort: public first, then by name
        channels.sort(key=lambda c: (c.get("type", ""), c.get("display_name", "")))
        for ch in channels:
            ch_type = ch.get("type", "?")
            if ch_type == "D":
                continue  # skip DMs
            if ch_type == "G":
                continue  # skip group messages
            label = "public" if ch_type == "O" else "private" if ch_type == "P" else ch_type
            print(f"  [{ch['id']}] {ch['display_name']} ({label})")

    # Summary
    print("\n" + "=" * 60)
    print("Добавь в env.local:")
    print(f"  MATTERMOST_BOT_USER_ID={mm.bot_user_id}")
    if teams:
        print(f"  MATTERMOST_TEAM_ID={teams[0]['id']}  # {teams[0]['display_name']}")
    print(f"  DATA_CONTRACTS_CHANNEL_ID=<id канала из списка выше>")
    print("=" * 60)


if __name__ == "__main__":
    main()
