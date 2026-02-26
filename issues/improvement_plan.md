# План улучшений агента

Последнее обновление: 2026-02-26

---

## P0 — Баги и надёжность

### 1. Таймаут LLM-вызовов в agentic loop
`src/llm_client.py` — `call_with_tools()` не имеет таймаута на каждый turn. Если LLM зависнет, тред listener-а заблокирован навсегда. Добавить `timeout=` в `client.chat.completions.create()` + catch `openai.Timeout`.

### 2. Race condition в dedup-логике listener
`src/listener.py` — блокировка `_dedup_lock` снимается до начала обработки сообщения. Если WebSocket доставит дубль в зазор между `_inflight_post_ids.add()` и реальной обработкой — возможна двойная обработка. Нужно либо обрабатывать внутри lock, либо перейти на очередь (`queue.Queue`).

### 3. Нет retry для file I/O
`src/memory.py` — все write-операции однократные. Если диск временно недоступен (Docker volume, сеть), агент крашится. Добавить retry с backoff для `write_file()`, `write_json()`.

---

## P1 — Тесты

### 4. Нет тестов для glossary.py
`src/glossary.py` — `check_ambiguity()` вызывается из `save_contract()`, но ни одного теста нет. Нужны тесты на: нормальный кейс, пустой глоссарий, частичное совпадение.

### 5. Нет тестов для analyzer.py
`src/analyzer.py` — conflict detection, cycle detection, Jaccard similarity — всё без тестов. При изменении логики нет safety net.

### 6. Нет интеграционного теста listener → agent → reply
End-to-end тест: WebSocket event → `agent.process_message()` → tool calls → Mattermost reply. Сейчас тестируются только отдельные компоненты.

### 7. Нет тестов для scheduler
`src/scheduler.py` — reminder escalation (4 ступени) + digest + coverage scan — всё без тестов.

---

## P2 — Фичи для UX

### 8. Команда `approve_contract`
Сейчас согласование — это ручное добавление `@username` в секцию "Согласовано" контракта. Нужен интерактивный workflow: агент собирает апрувы через реакции или явные команды, проверяет governance policy, и финализирует когда кворум достигнут.

### 9. Автогенерация шаблона нового контракта
При `начни контракт X` агент сразу мог бы генерировать skeleton с предзаполненными полями (метрика из дерева, путь к корню, stakeholders из circles.md, governance tier). Сейчас LLM делает это каждый раз с нуля.

### 10. Diff между версиями контракта
`покажи версию X <timestamp>` работает, но формат timestamp неудобный и нет возможности сравнить две версии. Добавить `покажи diff <contract_id>` — показать разницу между текущей и предыдущей версией.

### 11. Нормализация терминологии в промптах
В промптах и коде mixed: "draft"/"черновик", "agreed"/"согласован", англ./рус. Привести к единой терминологии в пользовательском интерфейсе.

---

## P3 — Архитектура и масштабирование

### 12. Конфигурируемые константы
Hardcoded значения разбросаны по коду: `_THREAD_MAX_MESSAGES = 15`, `_THREAD_MAX_CHARS = 4000`, `_DEDUP_TTL_SECONDS`, governance `days_threshold = 180`. Вынести в env-переменные или единый config.

### 13. Audit log для всех мутаций
Сейчас только `memory/decisions.jsonl` для решений. Нет записи: кто сменил статус, кто назначил роль, когда удалён reminder. Добавить единый audit trail.

### 14. O(n²) в conflict detection
`src/analyzer.py` — попарное сравнение всех контрактов через Jaccard similarity. При 50+ контрактах станет медленным. Нужен индекс или инкрементальный анализ (проверять только новый контракт vs существующие).

### 15. Транзакционность записи
`save_contract()` делает 5+ записей подряд (файл, index, versions, history, relationships, tree). Если крашнется на середине — inconsistent state. Нужен хотя бы write-ahead pattern или atomic rename.

---

## P4 — Nice to have

### 16. Аналитика участников
Кто отвечает быстрее, кто блокирует, средние сроки согласования.

### 17. Очистка старых тредов
`active_threads.json` растёт бесконечно, TTL есть при чтении, но записи не удаляются.

### 18. Рефакторинг system prompt
`prompts/system_full.md` (232 строки) содержит повторы. Сократить и структурировать — экономия токенов на каждом вызове.
