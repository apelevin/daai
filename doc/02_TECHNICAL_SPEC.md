# Техническая спецификация: AI-архитектор метрик (MVP)

## Scope MVP

Агент формирует Data Contracts через переписку в Mattermost. Без интеграции с базой данных. Результат: набор согласованных Data Contracts в виде markdown-файлов.

---

## Архитектура

```
┌──────────────────────────────────────────┐
│              AGENT SERVICE               │
│                                          │
│  ┌──────────┐  ┌──────────┐  ┌────────┐ │
│  │ Listener │  │ Router   │  │Scheduler│ │
│  │(Mattermost│  │(cheap    │  │(cron   │ │
│  │ WebSocket)│  │ model)   │  │задачи) │ │
│  └─────┬────┘  └─────┬────┘  └───┬────┘ │
│        │             │           │       │
│        └──────┬──────┘───────────┘       │
│               ▼                          │
│        ┌─────────────┐                   │
│        │ OpenRouter   │                   │
│        │ (cheap/heavy)│                   │
│        └──────┬──────┘                   │
│               │                          │
│        ┌──────┴──────┐                   │
│        ▼             ▼                   │
│   ┌────────┐   ┌──────────┐              │
│   │ Files  │   │Mattermost│              │
│   │(контекст│   │   API    │              │
│   │ память)│   │          │              │
│   └────────┘   └──────────┘              │
└──────────────────────────────────────────┘
```

---

## Модели и OpenRouter

### Принцип разделения

**Cheap Model** (быстрые, дешёвые операции — без уточнений, без переспрашиваний):
- Роутинг: классификация входящего сообщения
- Напоминания: генерация стандартного напоминания по шаблону
- Статус-запросы: «покажи все контракты», «какой статус у churn»
- Простые ответы: когда вся информация есть в файлах

**Heavy Model** (сложные, дорогие операции):
- Формулирование вопросов интервью
- Анализ противоречий между ответами stakeholder'ов
- Формирование предложения для консента
- Генерация и обновление Data Contract
- Обновление профилей участников (анализ паттернов)
- Еженедельный дайджест

### Конфигурация OpenRouter

```python
OPENROUTER_API_KEY = "sk-or-..."
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Cheap model — для роутинга и простых задач
CHEAP_MODEL = "anthropic/claude-haiku"  # или google/gemini-flash

# Heavy model — для аналитики и генерации
HEAVY_MODEL = "anthropic/claude-sonnet"  # или anthropic/claude-opus
```

### Вызов через OpenRouter (OpenAI-совместимый API)

```python
import openai

client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

def call_cheap(system_prompt, user_message):
    """Быстрый дешёвый вызов. Без tool use."""
    response = client.chat.completions.create(
        model=CHEAP_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=500,
        temperature=0.0,
    )
    return response.choices[0].message.content

def call_heavy(system_prompt, user_message, max_tokens=2000):
    """Тяжёлый вызов для аналитики и генерации."""
    response = client.chat.completions.create(
        model=HEAVY_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    return response.choices[0].message.content
```

---

## Router: классификация сообщений

Router использует **cheap model**. Задача: за один вызов определить тип запроса и что подгрузить. Без уточняющих вопросов — всегда даёт ответ сразу.

### Промпт роутера

```
Классифицируй сообщение. Верни только JSON, ничего больше.

Сообщение от @{username} в {channel|dm}:
"{message_text}"

{thread_context_if_any}

Категории:
- contract_request: просит показать конкретный контракт
- status_request: просит список/статус контрактов
- contract_discussion: ответ в треде согласования контракта
- problem_report: сообщает о расхождении/проблеме с данными
- new_contract_init: просит начать новый контракт
- profile_intro: новый участник представляется
- general_question: общий вопрос о данных или метриках
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
- heavy: contract_discussion, problem_report, new_contract_init, 
         general_question, profile_intro
```

### Логика выбора модели

```python
CHEAP_TYPES = {"contract_request", "status_request", "irrelevant"}
HEAVY_TYPES = {"contract_discussion", "problem_report", 
               "new_contract_init", "general_question", "profile_intro"}

def process_message(username, message, channel_type, thread_context):
    # 1. Router (cheap)
    route = call_cheap(ROUTER_PROMPT, format_input(...))
    route_data = json.loads(route)
    
    # 2. Загрузить файлы
    context_files = load_files(route_data["load_files"])
    
    # 3. Вызвать нужную модель
    if route_data["model"] == "cheap":
        response = call_cheap(
            system_prompt=SYSTEM_PROMPT_SHORT + context_files,
            user_message=message
        )
    else:
        response = call_heavy(
            system_prompt=SYSTEM_PROMPT_FULL + context_files,
            user_message=message + thread_context
        )
    
    # 4. Обработать side effects (сохранение файлов)
    handle_side_effects(response, route_data)
    
    return response
```

---

## Компоненты

### 1. Listener (mattermost_client.py)

WebSocket-подключение к Mattermost. Слушает события.

```python
# Ключевые события
"posted"     → новое сообщение → process_message()
"user_added" → новый участник → onboard_participant()
```

**Mattermost API:**
```
WebSocket: wss://{server}/api/v4/websocket
POST /api/v4/posts                     — отправить сообщение
GET  /api/v4/channels/{id}/posts       — сообщения канала
GET  /api/v4/users/{id}                — информация о пользователе
GET  /api/v4/channels/{id}/members     — участники канала
GET  /api/v4/posts/{id}/thread         — получить тред
```

### 2. Scheduler (scheduler.py)

```python
SCHEDULE = {
    "check_reminders": "*/4 * * * *",     # Каждые 4 часа
    "weekly_digest":   "0 17 * * 5",      # Пятница 17:00
}
```

**check_reminders:** Читает reminders.json → для просроченных генерирует напоминание (cheap model для стандартных шагов, heavy для эскалации) → отправляет в Mattermost → обновляет reminders.json.

**weekly_digest:** Читает index.json, queue.json → генерирует дайджест (heavy model) → публикует в канал.

### 3. Agent (agent.py)

Основная логика. Для каждого сообщения:
1. Router (cheap) → тип + файлы
2. Загрузка контекста из файлов
3. Вызов cheap или heavy модели
4. Отправка ответа в Mattermost
5. Сохранение в память (файлы)

### 4. Memory (memory.py)

Работа с файловой системой. Все данные — файлы на диске.

```python
def save_contract(contract_id, content)        # Записать/обновить контракт
def save_draft(contract_id, content)           # Записать драфт
def save_discussion(contract_id, summary)      # Обновить резюме обсуждения
def save_decision(contract_id, decision_data)  # Записать решение
def update_participant(username, updates)       # Обновить профиль
def update_contract_index(contract_id, data)   # Обновить реестр
def add_reminder(reminder_data)                # Добавить напоминание
def update_reminder(reminder_id, updates)      # Обновить напоминание
def remove_reminder(reminder_id)               # Удалить напоминание
def read_file(path)                            # Прочитать файл
def list_contracts()                           # Список всех контрактов
```

---

## Структура файлов

```
ai-architect/
│
├── .env                              # Credentials
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
│
├── src/
│   ├── main.py                       # Точка входа
│   ├── listener.py                   # Mattermost WebSocket
│   ├── router.py                     # Классификация (cheap model)
│   ├── agent.py                      # Основная логика
│   ├── scheduler.py                  # Cron-задачи
│   ├── mattermost_client.py          # Обёртка Mattermost API
│   ├── llm_client.py                 # Обёртка OpenRouter (cheap/heavy)
│   └── memory.py                     # Работа с файлами
│
├── prompts/
│   ├── system_full.md                # Полный system prompt (heavy)
│   ├── system_short.md               # Сокращённый (cheap — только ответы)
│   ├── router.md                     # Промпт роутера
│   ├── reminder_templates.md         # Шаблоны напоминаний по шагам
│   └── digest_template.md            # Шаблон дайджеста
│
├── context/                          # Бизнес-контекст
│   ├── company.md                    # Компания, продукты, стратегия
│   ├── circles.md                    # Круги, домены, метрики
│   └── metrics_tree.md               # Дерево метрик от Extra Time
│
├── contracts/                        # Согласованные Data Contracts
│   ├── index.json                    # Реестр
│   └── ...
│
├── drafts/                           # В работе
│   ├── {name}.md                     # Драфт контракта
│   └── {name}_discussion.jsonl       # Резюме обсуждения
│
├── participants/                     # Профили
│   ├── index.json
│   └── {username}.md
│
├── memory/
│   └── decisions.jsonl               # Журнал решений
│
└── tasks/
    ├── queue.json                    # Очередь контрактов
    └── reminders.json                # Напоминания
```

---

## Форматы файлов

### .env

```bash
# Mattermost
MATTERMOST_URL=https://mattermost.pravotech.ru
MATTERMOST_BOT_TOKEN=xxxxxxxxxxxxxxxxxxxx
MATTERMOST_BOT_USER_ID=bot_user_id_here
DATA_CONTRACTS_CHANNEL_ID=channel_id_here

# OpenRouter
OPENROUTER_API_KEY=sk-or-xxxxxxxxxxxxxxxxxxxxx
CHEAP_MODEL=anthropic/claude-3-5-haiku-20241022
HEAVY_MODEL=anthropic/claude-sonnet-4-5-20250929

# Агент
ESCALATION_USER=alexey
CONSENT_SILENCE_DAYS=3
REMINDER_CHECK_HOURS=4
```

### contracts/index.json

```json
{
  "contracts": [
    {
      "id": "win_ni",
      "name": "WIN NI",
      "status": "agreed",
      "file": "contracts/win_ni.md",
      "agreed_by": ["sales_lead", "dd_lead"],
      "agreed_date": "2026-03-05",
      "stakeholders": ["sales_lead", "dd_lead"],
      "thread_id": "mm_post_id_here"
    },
    {
      "id": "churn_rate",
      "name": "Churn Rate",
      "status": "in_progress",
      "file": "drafts/churn_rate.md",
      "discussion_file": "drafts/churn_rate_discussion.jsonl",
      "stakeholders": ["sales_lead", "product_lead"],
      "blocker": "product_lead",
      "blocker_since": "2026-03-10",
      "thread_id": "mm_post_id_here"
    }
  ]
}
```

### tasks/queue.json

```json
{
  "queue": [
    {
      "id": "win_ni",
      "priority": 1,
      "reason": "Расхождение 27.5 vs 31 млн между бордом и выгрузкой",
      "stakeholders": ["sales_lead", "dd_lead"],
      "status": "in_progress"
    },
    {
      "id": "churn_rate",
      "priority": 2,
      "reason": "Sales и Product считают по-разному",
      "stakeholders": ["sales_lead", "product_lead"],
      "status": "queued"
    }
  ]
}
```

### tasks/reminders.json

```json
{
  "reminders": [
    {
      "id": "rem_001",
      "contract_id": "churn_rate",
      "target_user": "product_lead",
      "target_mm_user_id": "mm_user_id",
      "thread_id": "mm_post_id",
      "question_summary": "Согласен ли с разделением на Contract Churn и Usage Churn?",
      "first_asked": "2026-03-10T09:00:00Z",
      "last_reminder": "2026-03-12T09:00:00Z",
      "escalation_step": 1,
      "next_reminder": "2026-03-14T09:00:00Z"
    }
  ]
}
```

### participants/{username}.md

```markdown
# Иванов Алексей (@ivanov)

## Базовое
- Круг: Sales
- Роль: руководитель продаж
- В канале с: 2026-03-01

## Домен и данные
- Метрики: WIN NI, конверсия воронки
- Боли: «цифры на борде не совпадают с CRM»

## Профиль коммуникации
- Скорость ответа: ~4 часа
- Предпочитает: варианты на выбор (не открытые вопросы)
- Аргументирует: через примеры клиентов

## Компетенции
- Сильные: знание клиентов, практические последствия
- Зоны развития: путает pipeline с WIN

## Позиции по контрактам
- WIN NI: «Только подписанные контракты текущего квартала»

## История
- 2026-03-05: Согласовал WIN NI
```

### drafts/{name}_discussion.jsonl

```json
{
  "entity": "churn_rate",
  "status": "in_progress",
  "updated": "2026-03-12",
  "positions": {
    "sales_lead": "Churn = непродление контракта в 30 дней",
    "product_lead": "Churn = MAU ниже 20% от пика"
  },
  "proposed_resolution": "Два показателя: Contract Churn и Usage Churn",
  "blocker": "product_lead",
  "next_action": "Напомнить 2026-03-14"
}
```

### memory/decisions.jsonl

```json
{"date": "2026-03-05", "contract": "win_ni", "decision": "WIN NI = только подписанные контракты текущего квартала, валидированные в CRM", "agreed_by": ["sales_lead", "dd_lead"], "method": "consent", "thread_id": "mm_post_id"}
```

---

## Оптимизация стоимости: шаблоны для cheap model

Напоминания не нужно генерировать каждый раз — это шаблоны с подстановкой.

### prompts/reminder_templates.md

```markdown
## Шаг 1 (день 2): Мягкое напоминание
Шаблон для канала (тред):
«@{username}, напоминаю — жду твоё мнение по {contract_name}. 
Можешь ответить коротко, даже одним предложением.»

## Шаг 2 (день 4): Упрощение
Шаблон для канала (тред):
«@{username}, упрощу. Два варианта:
A — {option_a}
B — {option_b}
Напиши A или B, я дальше сам оформлю.»

## Шаг 3 (день 6): Личное сообщение
Шаблон для DM:
«Привет. В канале Data Contracts жду твой ответ по {contract_name} — 
это блокирует согласование. Можешь ответить прямо здесь.»

## Шаг 4 (день 8): Эскалация
Шаблон для канала:
«@{escalation_user}, контракт {contract_name} заблокирован {days} дней. 
Жду ответа от @{username}. Нужна помощь.»
```

Шаги 1, 3, 4 — чистые шаблоны, **не требуют LLM вообще** (строковая подстановка). Шаг 2 требует cheap model только если варианты A/B не зафиксированы в discussion.jsonl.

---

## Потоки данных

### Поток 1: Сообщение в канале

```
WebSocket event → listener.py
  → router.py (cheap model → тип + файлы + cheap/heavy)
  → agent.py (загрузка файлов → вызов модели → ответ)
  → mattermost_client.py (отправка в канал/тред)
  → memory.py (сохранение)
```

### Поток 2: Личное сообщение

```
WebSocket event → listener.py
  → router.py (cheap → тип)
  → agent.py (загрузка → модель → ответ)
  → mattermost_client.py (отправка в DM)
```

### Поток 3: Напоминание

```
scheduler.py → check_reminders()
  → читает reminders.json
  → для просроченных:
    шаги 1,3,4 → шаблонная подстановка (бесплатно)
    шаг 2 → cheap model (если нужны варианты)
  → mattermost_client.py (отправка)
  → обновление reminders.json
```

### Поток 4: Новый участник

```
WebSocket event "user_added" → listener.py
  → получить инфу через Mattermost API
  → создать базовый профиль (шаблон, без LLM)
  → отправить приветствие в DM (шаблон)
  → ждать ответа → при получении → heavy model для парсинга профиля
```

---

## Docker

### docker-compose.yml

```yaml
version: '3.8'

services:
  agent:
    build: .
    env_file: .env
    volumes:
      - ./prompts:/app/prompts:ro
      - ./context:/app/context:ro
      - ./contracts:/app/contracts
      - ./drafts:/app/drafts
      - ./participants:/app/participants
      - ./memory:/app/memory
      - ./tasks:/app/tasks
    restart: unless-stopped
```

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

CMD ["python", "src/main.py"]
```

### requirements.txt

```
openai>=1.50.0
mattermostdriver>=7.3.2
schedule>=1.2.0
python-dotenv>=1.0.0
```

---

## Оценка стоимости

### Предположения
- 20–30 сообщений/день в канале и личке
- 5 контрактов в работе одновременно
- 6 напоминаний/день

### Расход токенов/день

| Операция | Модель | Вызовов/день | Токены/вызов | Итого токенов |
|---|---|---|---|---|
| Роутинг | Cheap | 30 | ~800 | 24,000 |
| Простые ответы | Cheap | 10 | ~1,500 | 15,000 |
| Интервью, анализ | Heavy | 10 | ~3,000 | 30,000 |
| Генерация контрактов | Heavy | 2 | ~4,000 | 8,000 |
| Напоминания | Шаблон | 6 | 0 | 0 |
| **Итого cheap** | | | | **~39,000** |
| **Итого heavy** | | | | **~38,000** |

### Примерная стоимость (OpenRouter)

- Haiku: ~$0.003 / день (cheap операции)
- Sonnet: ~$0.10 / день (heavy операции)
- **Итого: ~$3 / месяц** при умеренной нагрузке

Это грубая оценка. При активном согласовании 5+ контрактов одновременно — может вырасти до $10–15/мес. Всё равно копейки.
