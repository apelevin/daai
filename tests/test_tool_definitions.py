"""Tests for tool_definitions.py — validate all tool schemas are well-formed."""

import unittest

from src.tool_definitions import get_read_tools, get_write_tools, get_all_tools


class ToolDefinitionsTest(unittest.TestCase):
    def test_all_tools_have_required_fields(self):
        for tool in get_all_tools():
            self.assertEqual(tool["type"], "function", f"Tool missing type=function: {tool}")
            func = tool.get("function")
            self.assertIsInstance(func, dict, f"Tool missing function dict: {tool}")
            self.assertIn("name", func)
            self.assertIn("description", func)
            self.assertIn("parameters", func)
            params = func["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIn("properties", params)
            self.assertIn("required", params)

    def test_tool_names_unique(self):
        names = [t["function"]["name"] for t in get_all_tools()]
        self.assertEqual(len(names), len(set(names)), f"Duplicate tool names: {names}")

    def test_read_tools_count(self):
        self.assertEqual(len(get_read_tools()), 11)

    def test_write_tools_count(self):
        self.assertEqual(len(get_write_tools()), 11)

    def test_required_params_are_in_properties(self):
        for tool in get_all_tools():
            func = tool["function"]
            params = func["parameters"]
            props = set(params["properties"].keys())
            req = set(params["required"])
            self.assertTrue(
                req.issubset(props),
                f"Tool {func['name']}: required {req - props} not in properties",
            )

    def test_known_tool_names(self):
        expected_read = {
            "read_contract", "read_draft", "read_discussion",
            "read_governance_policy", "read_roles", "validate_contract",
            "check_approval", "diff_contract", "generate_contract_template",
            "participant_stats", "list_contracts",
        }
        expected_write = {
            "save_contract", "save_draft", "update_discussion",
            "add_reminder", "update_participant", "save_decision",
            "assign_role", "set_contract_status",
            "request_approval", "approve_contract", "ask_question",
        }
        read_names = {t["function"]["name"] for t in get_read_tools()}
        write_names = {t["function"]["name"] for t in get_write_tools()}
        self.assertEqual(read_names, expected_read)
        self.assertEqual(write_names, expected_write)


if __name__ == "__main__":
    unittest.main()
