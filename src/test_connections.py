"""Test script to verify Mattermost and OpenRouter connections."""

import os
import sys

from dotenv import load_dotenv

load_dotenv()
# Project uses env.local by convention
load_dotenv("env.local")

from src.mattermost_client import MattermostClient
from src.llm_client import LLMClient


def test_mattermost():
    print("=" * 50)
    print("Testing Mattermost connection...")
    print("=" * 50)

    mm = MattermostClient()
    print("[OK] Connected to Mattermost")

    # 1. Send test message to channel
    post = mm.send_to_channel("🔧 Тест: сообщение в канал")
    post_id = post["id"]
    print(f"[OK] Sent message to channel (post_id={post_id})")

    # 2. Send thread reply
    mm.send_to_channel("🔧 Тест: ответ в треде", root_id=post_id)
    print("[OK] Sent thread reply")

    # 3. Send DM to escalation user
    escalation_user = os.environ.get("ESCALATION_USER")
    if escalation_user:
        # Need to find user ID by username
        try:
            # Use driver directly to find user by username
            user = mm.driver.users.get_user_by_username(escalation_user)
            mm.send_dm(user["id"], "🔧 Тест: личное сообщение от бота")
            print(f"[OK] Sent DM to @{escalation_user}")
        except Exception as e:
            print(f"[WARN] Could not send DM to @{escalation_user}: {e}")
    else:
        print("[SKIP] ESCALATION_USER not set, skipping DM test")

    # 4. Get channel members
    members = mm.get_channel_members()
    print(f"[OK] Channel has {len(members)} members")
    for m in members[:5]:
        print(f"     - @{m['username']} ({m['display_name']})")

    print()


def test_openrouter():
    print("=" * 50)
    print("Testing OpenRouter connection...")
    print("=" * 50)

    llm = LLMClient()
    print(f"[OK] LLM client initialized (cheap={llm.cheap_model}, heavy={llm.heavy_model})")

    # 1. Test cheap model
    response = llm.call_cheap(
        "Ответь одним словом на русском.",
        "Работает ли это соединение?"
    )
    print(f"[OK] Cheap model response: {response.strip()}")

    # 2. Test heavy model
    response = llm.call_heavy(
        "Ответь одним предложением на русском.",
        "Скажи 'heavy model работает'"
    )
    print(f"[OK] Heavy model response: {response.strip()}")

    print()


def main():
    success = True

    try:
        test_mattermost()
    except Exception as e:
        print(f"[FAIL] Mattermost: {e}")
        success = False

    try:
        test_openrouter()
    except Exception as e:
        print(f"[FAIL] OpenRouter: {e}")
        success = False

    if success:
        print("=" * 50)
        print("All tests passed!")
        print("=" * 50)
    else:
        print("=" * 50)
        print("Some tests failed. Check configuration.")
        print("=" * 50)
        sys.exit(1)


if __name__ == "__main__":
    main()
