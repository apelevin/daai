import argparse
import json
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="Keep temp DATA_DIRs created by runners")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    scenarios = [
        ("01_smoke", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/01_smoke.json")] + (["--keep"] if args.keep else [])),
        ("02_contract_request_missing", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/02_contract_request_missing.json")] + (["--keep"] if args.keep else [])),
        ("03_contract_request_present", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/03_contract_request_present.json")] + (["--keep"] if args.keep else [])),
        ("04_win_ni_discussion_ab", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/04_win_ni_discussion_ab.json")] + (["--keep"] if args.keep else [])),
        ("05_win_ni_save_contract", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/05_win_ni_save_contract.json")] + (["--keep"] if args.keep else [])),
        ("06_win_ni_full_e2e", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/06_win_ni_full_e2e.json")] + (["--keep"] if args.keep else [])),
        ("07_reminders_steps_1_4", [sys.executable, str(root / "run_reminders_steps.py"), str(root / "scenarios/07_reminders_steps_1_4.json")] + (["--keep"] if args.keep else [])),
        ("08_onboarding_dm_profile_update", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/08_onboarding_dm_profile_update.json")] + (["--keep"] if args.keep else [])),
        ("09_contract_versioning_history", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/09_contract_versioning_history.json")] + (["--keep"] if args.keep else [])),
        ("10_user_added_removed_participants_index", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/10_user_added_removed_participants_index.json")] + (["--keep"] if args.keep else [])),
        ("11_validator_blocks_invalid_save_contract", [sys.executable, str(root / "run_scenarios.py"), str(root / "scenarios/11_validator_blocks_invalid_save_contract.json")] + (["--keep"] if args.keep else [])),
        ("12_metrics_tree_mark_check", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/12_metrics_tree_mark_check.json"), "--check", "context/metrics_tree.md", "--contains", "✅"]),
        ("13_conflicts_audit_same_name_diff_formula", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/13_conflicts_audit_same_name_diff_formula.json"), "--transcript-contains", "Конфликт формулы"]),
        ("14_conflicts_audit_cycle_related_contracts", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/14_conflicts_audit_cycle_related_contracts.json"), "--transcript-contains", "Циклическая зависимость"]),
        ("15_conflicts_audit_missing_formula", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/15_conflicts_audit_missing_formula.json"), "--transcript-contains", "Нет формулы"]),
        ("16_conflicts_audit_missing_definition_data_source", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/16_conflicts_audit_missing_definition_data_source.json"), "--transcript-contains", "Нет определения"]),
        ("17_conflicts_audit_unknown_related_and_ambiguous_formula", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/17_conflicts_audit_unknown_related_and_ambiguous_formula.json"), "--transcript-contains", "Неоднозначная формула"]),
        ("18_conflicts_audit_bad_extra_time_path_start_end", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/18_conflicts_audit_bad_extra_time_path_start_end.json"), "--transcript-contains", "не начинается с метрики"]),
        ("19_conflicts_audit_name_normalization_hyphen", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/19_conflicts_audit_name_normalization_hyphen.json"), "--transcript-contains", "Конфликт формулы"]),
        ("20_conflicts_audit_overlapping_definitions", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/20_conflicts_audit_overlapping_definitions.json"), "--transcript-contains", "пересекающиеся определения"]),
        ("21_glossary_blocks_ambiguous_client", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/21_glossary_blocks_ambiguous_client.json"), "--transcript-contains", "нужно уточнение терминов"]),
        ("22_relationships_mentions_saved_on_save_contract", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/22_relationships_mentions_saved_on_save_contract.json"), "--check", "contracts/relationships.json", "--contains", "\"to\": \"dim_customer\""]),
        ("23_relationships_llm_semantic_saved", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/23_relationships_llm_semantic_saved.json"), "--check", "contracts/relationships.json", "--contains", "\"type\": \"depends_on\""]),
        ("24_relationships_llm_invalid_ignored", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/24_relationships_llm_invalid_ignored.json"), "--check", "contracts/relationships.json", "--contains", "\"type\": \"mentions\""]),
        ("25_relationships_show_command", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/25_relationships_show_command.json"), "--transcript-contains", "depends_on"]),
        ("26_governance_review_audit_old_contract", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/26_governance_review_audit_old_contract.json"), "--transcript-contains", "требующие пересмотра"]),
        ("27_governance_tier1_blocks_without_required_roles", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/27_governance_tier1_blocks_without_required_roles.json"), "--transcript-contains", "политика согласования"]),
        ("28_governance_show_policy_command", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/28_governance_show_policy_command.json"), "--transcript-contains", "Политика согласования tier_2"]),
        ("29_governance_requirements_for_contract", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/29_governance_requirements_for_contract.json"), "--transcript-contains", "Требования согласования"]),
        ("30_lifecycle_get_status", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/30_lifecycle_get_status.json"), "--transcript-contains", "in_review"]),
        ("31_lifecycle_set_status", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/31_lifecycle_set_status.json"), "--transcript-contains", "статус теперь"]),
        ("32_lifecycle_auto_in_review_on_discussion", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/32_lifecycle_auto_in_review_on_discussion.json"), "--check", "contracts/index.json", "--contains", "\"status\": \"in_review\""]),
        ("33_core_flow_negotiation_conflict_to_consent_save", [sys.executable, str(root / "run_scenario_check.py"), str(root / "scenarios/33_core_flow_negotiation_conflict_to_consent_save.json"), "--check", "contracts/relationships.json", "--contains", "\"type\": \"depends_on\""]),
        ("34_dunning_ladder_silent_stakeholder", [sys.executable, str(root / "run_reminders_check.py"), str(root / "scenarios/34_dunning_ladder_silent_stakeholder.json"),
            "--contains", "@sales_lead, напоминаю",
            "--contains", "Два варианта",
            "--contains", "Привет. В канале Data Contracts",
            "--contains", "@controller"
        ]),
        ("35_reminder_templates_source_of_truth", [sys.executable, str(root / "run_reminders_check.py"), str(root / "scenarios/35_reminder_templates_source_of_truth.json"),
            "--contains", "MAGIC_TEMPLATE_123"
        ]),
        ("36_reminder_templates_all_steps", [sys.executable, str(root / "run_reminders_check.py"), str(root / "scenarios/36_reminder_templates_all_steps.json"),
            "--contains", "MAGIC_SOFT",
            "--contains", "MAGIC_AB",
            "--contains", "MAGIC_DM",
            "--contains", "MAGIC_ESC"
        ]),
        ("37_reminder_templates_placeholders", [sys.executable, str(root / "run_reminders_check.py"), str(root / "scenarios/37_reminder_templates_placeholders.json"),
            "--contains", "SOFT>> @sales_lead / win_ni / Когда фиксируем WIN NI",
            "--contains", "DM>> sales_lead / win_ni",
            "--contains", "ESC>> @controller",
            "--contains", "AB>>"
        ]),
    ]

    results = []
    for name, cmd in scenarios:
        code, out = run(cmd)
        ok = code == 0
        results.append({"name": name, "ok": ok, "code": code})
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        if not ok:
            print(out)

    all_ok = all(r["ok"] for r in results)
    print("\nSummary:")
    print(json.dumps(results, ensure_ascii=False, indent=2))

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
