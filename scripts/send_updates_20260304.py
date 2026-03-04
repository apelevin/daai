#!/usr/bin/env python3
"""Send status update messages to active contract threads.

Run inside the container:
  docker compose exec agent python scripts/send_updates_20260304.py

Or on host with env:
  cd /opt/apps/daai && docker compose exec agent python -c "
  import sys; sys.path.insert(0,'.'); exec(open('scripts/send_updates_20260304.py').read())
  "
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.mattermost_client import MattermostClient


UPDATES = [
    {
        "thread_id": "ggxa84w713bk38uf5483ro6x9r",  # win_rec
        "message": (
            "📋 **Статус контракта `win_rec` (WIN REC)**\n\n"
            "Все ключевые участники согласовали определение и формулу:\n"
            "- @l.khagush — согласована\n"
            "- @korabovtsev — согласован, уточнил логику даты пролонгации\n"
            "- @pelevin — согласован\n\n"
            "@pavelpetrin — ждём твоё подтверждение по готовности дашбордов. "
            "После этого фиксируем контракт.\n\n"
            "Есть возражения или дополнения?"
        ),
    },
    {
        "thread_id": "m1q1s3j6ztbxu853yckhdqrihc",  # recurring_income
        "message": (
            "📋 **Статус контракта `recurring_income` (REC)**\n\n"
            "Определение и источник данных зафиксированы от @Эвелина Царегородцева (Лучинская).\n\n"
            "Открытый вопрос: **кто будет ответственным за расчёт REC?**\n"
            "Нужен человек, который отвечает за формулы и корректность вычислений.\n\n"
            "@pelevin — можешь предложить кандидата или назначить?"
        ),
    },
    {
        "thread_id": "9ux4janwz7b8pm6hre768ixqgr",  # extra_time_dashboards
        "message": (
            "📋 **Статус контракта `extra_time_dashboards`**\n\n"
            "Концепция согласована: дашборды Extra Time по продуктам (Casebook, Caselook, Casebook API). "
            "@y.ilchuk и @pavelpetrin подтвердили готовность.\n\n"
            "Ждём ответы от продуктовых команд:\n"
            "- Какой статус у «Карточек работы» в ваших продуктах?\n"
            "- Готовы ли участвовать в предоставлении данных?\n\n"
            "@s.bankovskii @m.sitnikov @ivan.okuskov @a.kotlyar @a.pervitskii — напишите, пожалуйста, в этом треде."
        ),
    },
]


def main():
    print("Connecting to Mattermost...")
    mm = MattermostClient()
    print(f"Logged in as @{mm._username}\n")

    for upd in UPDATES:
        thread_id = upd["thread_id"]
        message = upd["message"]
        print(f"Sending to thread {thread_id}...")
        resp = mm.send_to_channel(message, root_id=thread_id)
        print(f"  -> post_id: {resp.get('id', '?')}\n")

    print("Done! All updates sent.")


if __name__ == "__main__":
    main()
