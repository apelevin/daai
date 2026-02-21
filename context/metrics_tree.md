# Дерево метрик (каркас для Data Contracts)

Дерево метрик — это каркас, к которому привязывается каждый **Data Contract**.
Без него контракты будут разрозненными документами. С ним — каждый контракт имеет место в системе.

## Корень

**Extra Time**

```
Extra Time = Saved_Time_per_Job × Jobs_per_User × MAU
```

## Дерево

```
Extra Time
├── MAU (Monthly Active Users)
│   ├── New Clients (acquisition)
│   │   ├── WIN NI (New Income от новых клиентов) ← DATA CONTRACT
│   │   ├── WIN REC (Recurring от новых клиентов) ← DATA CONTRACT
│   │   ├── Leads (входящий поток)
│   │   ├── Conversion Rate (лид → сделка)
│   │   └── Sales Cycle Length (время до закрытия)
│   ├── Retention (не уходят)
│   │   ├── Contract Churn (непродление контракта) ← DATA CONTRACT
│   │   ├── Usage Churn (падение MAU ниже порога) ← DATA CONTRACT
│   │   ├── ARR at Risk (клиенты с признаками ухода)
│   │   └── NPS / CSAT
│   └── Activation (начинают пользоваться)
│       ├── Activation Rate (% активированных лицензий) ← DATA CONTRACT
│       ├── Time-to-Value (время до первого результата)
│       └── Onboarding Completion Rate
├── Jobs per User (задач на пользователя)
│   ├── Adoption (используют больше)
│   │   ├── Feature Adoption Rate (по фичам/продуктам)
│   │   ├── Cross-sell Rate (клиенты с 2+ продуктами)
│   │   └── Jobs per Session (задач за сессию)
│   └── Engagement (возвращаются чаще)
│       ├── DAU/MAU Ratio
│       ├── Session Frequency
│       └── Return Rate (после первой недели)
├── Saved Time per Job (экономия на задачу)
│   ├── Product Quality
│   │   ├── Search Relevance (точность поиска)
│   │   ├── AI Accuracy (точность AI-ответов)
│   │   └── Error Rate
│   └── UX / Speed
│       ├── Task Completion Time
│       ├── Page Load Time
│       └── Steps to Result
└── Revenue (следствие Extra Time)
    ├── New Income (NI) ← DATA CONTRACT
    │   ├── NI by Product (Casebook, Caselook, Case.one)
    │   └── NI by Segment (Enterprise, SMB, Solo)
    ├── Recurring Income (REC) ← DATA CONTRACT
    │   ├── REC by Product
    │   └── REC by Segment
    ├── ARPU (Average Revenue per User)
    └── EBITDA
        ├── Revenue (NI + REC)
        ├── COGS
        ├── OPEX
        │   ├── Cost-to-Serve (стоимость обслуживания)
        │   ├── CAC (стоимость привлечения)
        │   └── R&D
        └── Margin
```

## Как использовать дерево

### 1) Каждый Data Contract привязан к узлу дерева
В секции контракта **«Связь с Extra Time»** фиксируем путь по дереву.

Примеры:

- **WIN NI:**
  - `WIN NI → New Clients → MAU → Extra Time`
  - Рост WIN NI означает новых клиентов → растёт MAU → растёт Extra Time.

- **Contract Churn:**
  - `Contract Churn → Retention → MAU → Extra Time`
  - Снижение churn сохраняет базу → поддерживает MAU → поддерживает Extra Time.

- **Activation Rate:**
  - `Activation Rate → Activation → MAU → Extra Time`
  - Неактивированная лицензия = клиент не получает Extra Time.

### 2) Приоритет контрактов определяется деревом
Контракты ближе к корню (Extra Time) — приоритетнее.

Это не значит, что листовые метрики не нужны — но порядок согласования идёт сверху вниз.

## Приоритет контрактов для MVP

| Приоритет | Контракт | Узел дерева | Почему первый |
|---:|---|---|---|
| 1 | WIN NI | New Clients → MAU | Самая болезненная проблема (27.5 vs 31 млн) |
| 2 | Единый клиент (dim_customer) | Базовая сущность | Фундамент для всех остальных метрик |
| 3 | Contract Churn | Retention → MAU | Считается по-разному в Sales и Product |
| 4 | Activation Rate | Activation → MAU | Неактивированные лицензии = потерянный Extra Time |
| 5 | NI / REC (split) | Revenue | Нужно единое определение структуры выручки |
| 6 | Usage Churn | Retention → MAU | Опережающий индикатор contract churn |
| 7 | SLA Support | Saved Time per Job | Не включает DevOps, 92 тикета в слепой зоне |

## Статусы узлов дерева

Дерево — живой документ. Агент хранит его в `context/metrics_tree.md` и обновляет по мере согласования контрактов.

Правило: когда контракт согласован — соответствующий узел дерева получает статус **✅**.
