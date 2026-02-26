"""Tests for glossary.py — deterministic ambiguity checker."""

import pytest

from src.glossary import check_ambiguity, GlossaryIssue


GLOSSARY = {
    "terms": [
        {
            "canonical": "Клиент",
            "aliases": ["клиент", "customer", "account"],
            "disambiguation": {
                "Юрлицо": ["юридическое лицо", "юрлицо"],
                "Пользователь": ["пользователь", "user"],
            },
        },
        {
            "canonical": "Пользователь",
            "aliases": ["пользователь", "user"],
            "disambiguation": {
                "Физлицо": ["физическое лицо", "person"],
                "Сотрудник клиента": ["сотрудник", "employee"],
            },
        },
    ]
}


class TestCheckAmbiguity:
    def test_no_issues_when_disambiguated(self):
        """Term present + disambiguation keyword present → no issue."""
        contract = "Клиент — юридическое лицо, купившее продукт."
        issues = check_ambiguity(contract, GLOSSARY)
        assert len(issues) == 0

    def test_ambiguous_term_flagged(self):
        """Term present but no disambiguation keyword → issue raised."""
        contract = "Клиент обязан оплатить лицензию."
        issues = check_ambiguity(contract, GLOSSARY)
        assert len(issues) >= 1
        assert issues[0].canonical == "Клиент"
        assert "неоднозначно" in issues[0].message

    def test_alias_triggers_check(self):
        """Alias ('customer') should also trigger disambiguation check."""
        contract = "Each customer must renew annually."
        issues = check_ambiguity(contract, GLOSSARY)
        assert len(issues) >= 1
        assert issues[0].canonical == "Клиент"

    def test_disambiguation_via_alias_keyword(self):
        """Disambiguation keyword from alias ('user') satisfies the check."""
        contract = "Клиент — это user нашего продукта."
        issues = check_ambiguity(contract, GLOSSARY)
        client_issues = [i for i in issues if i.canonical == "Клиент"]
        assert len(client_issues) == 0

    def test_multiple_terms_checked(self):
        """Both terms can be ambiguous when no disambiguation keywords present."""
        # 'account' triggers Клиент, 'user' triggers Пользователь.
        # But 'user' also disambiguates Клиент → only Пользователь is ambiguous.
        # Use text that triggers both without cross-disambiguating.
        contract = "account и пользователь получают доступ к системе."
        issues = check_ambiguity(contract, GLOSSARY)
        canonicals = [i.canonical for i in issues]
        # "пользователь" disambiguates "Клиент" (it's a keyword in Пользователь group)
        # but "Пользователь" itself needs its own disambiguation (Физлицо/Сотрудник)
        assert "Пользователь" in canonicals

    def test_both_terms_ambiguous(self):
        """When disambiguation keywords are absent for both terms."""
        # Use only canonical + alias terms with no disambiguation keywords at all
        glossary = {
            "terms": [
                {
                    "canonical": "Метрика A",
                    "aliases": ["metric_a"],
                    "disambiguation": {
                        "Вариант 1": ["вариант_1"],
                        "Вариант 2": ["вариант_2"],
                    },
                },
                {
                    "canonical": "Метрика B",
                    "aliases": ["metric_b"],
                    "disambiguation": {
                        "Тип X": ["тип_x"],
                        "Тип Y": ["тип_y"],
                    },
                },
            ]
        }
        contract = "В контракте используется metric_a и metric_b."
        issues = check_ambiguity(contract, glossary)
        canonicals = [i.canonical for i in issues]
        assert "Метрика A" in canonicals
        assert "Метрика B" in canonicals

    def test_empty_glossary(self):
        assert check_ambiguity("Some text about клиент", None) == []
        assert check_ambiguity("Some text about клиент", {}) == []
        assert check_ambiguity("Some text about клиент", {"terms": []}) == []

    def test_empty_contract(self):
        assert check_ambiguity("", GLOSSARY) == []
        assert check_ambiguity(None, GLOSSARY) == []

    def test_no_matching_terms(self):
        """Contract without any glossary terms → no issues."""
        contract = "Метрика MAU считается по формуле X."
        issues = check_ambiguity(contract, GLOSSARY)
        assert len(issues) == 0

    def test_case_insensitive(self):
        """Matching is case-insensitive."""
        contract = "КЛИЕНТ обязан предоставить данные."
        issues = check_ambiguity(contract, GLOSSARY)
        assert len(issues) >= 1

    def test_term_without_disambiguation_skipped(self):
        """Terms without disambiguation dict are skipped."""
        glossary = {
            "terms": [
                {"canonical": "MAU", "aliases": ["mau"], "definition": "Monthly Active Users"},
            ]
        }
        contract = "MAU — это месячная метрика."
        issues = check_ambiguity(contract, glossary)
        assert len(issues) == 0

    def test_message_contains_options(self):
        """Issue message lists disambiguation options."""
        contract = "Клиент должен быть активен."
        issues = check_ambiguity(contract, GLOSSARY)
        msg = issues[0].message
        assert "Юрлицо" in msg
        assert "Пользователь" in msg
