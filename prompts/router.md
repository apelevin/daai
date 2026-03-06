Классифицируй сообщение. Верни только JSON, ничего больше.

Сообщение от @{username} в {channel_type}:
"{message_text}"

{thread_context}

Категории:
- contract_request: просит показать конкретный контракт
- status_request: просит список/статус контрактов
- contract_discussion: ответ в треде согласования контракта
- problem_report: сообщает о расхождении/проблеме с данными
- new_contract_init: просит начать новый контракт
- profile_intro: новый участник представляется
- data_query: вопрос по данным — запрос к базе данных, статистика, цифры, «сколько», «покажи данные», «выборка», «таблица», «метрика по факту»
- general_question: общий вопрос о данных или метриках (без запроса к БД)
- irrelevant: не относится к работе агента

JSON:
{
  "type": "contract_discussion",
  "entity": "win_ni",
  "load_files": ["contracts/win_ni.md", "participants/ivanov.md"],
  "model": "cheap|heavy"
}

Правила model:
- cheap: contract_request, status_request, irrelevant
- heavy: contract_discussion, problem_report, new_contract_init, general_question, profile_intro, data_query

Правила load_files:
- Если есть entity — загрузи соответствующий контракт или драфт
- Если упоминается пользователь — загрузи его профиль
- Для status_request — загрузи contracts/index.json
- Для general_question — загрузи context/company.md и context/metrics_tree.md
- Для data_query — load_files пустой (данные берутся из БД)
- Максимум 3 файла
