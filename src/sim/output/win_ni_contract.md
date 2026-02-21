# Data Contract: WIN NI

## Статус
Согласован

## Определение
WIN NI — сумма новых продаж по подписанным контрактам текущего квартала, валидированным в CRM.

## Формула
SUM(amount) WHERE signed_at in quarter AND is_new_sale = true

## Источник данных
CRM

## Исключения
Отменённые, неподписанные, barter.

## Согласовано
@sales_lead — 2026-02-19
@dd_lead — 2026-02-19
