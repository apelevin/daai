"""Create the Data Contracts channel and print its ID."""

import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "env.local"))

from src.mattermost_client import MattermostClient


def main():
    mm = MattermostClient()

    team_id = os.environ.get("MATTERMOST_TEAM_ID", "wrgkxtk6stncxkxar7m3oe96ur")

    channel = mm.driver.channels.create_channel({
        "team_id": team_id,
        "name": "data-contracts",
        "display_name": "Data Contracts",
        "purpose": "Согласование Data Contracts — единых определений метрик и данных",
        "header": "AI-архитектор метрик | Согласование определений данных",
        "type": "O",  # public
    })

    print(f"✅ Канал создан: {channel['display_name']}")
    print(f"   DATA_CONTRACTS_CHANNEL_ID={channel['id']}")
    print(f"\nДобавь в env.local:")
    print(f'   DATA_CONTRACTS_CHANNEL_ID="{channel["id"]}"')


if __name__ == "__main__":
    main()
