"""Ownership boundaries that must stay stable during future Agent/UX fixes."""
import ast
import unittest
from pathlib import Path
from agents.tools import model_receipt_for_tool

ROOT = Path(__file__).resolve().parents[1]


def imports(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}


class Stage4ArchitectureContracts(unittest.TestCase):
    def test_market_snapshot_has_no_high_layer_imports(self):
        self.assertNotIn("services.data_refresh", imports("services/market_snapshot.py"))
        self.assertNotIn("services.evidence", imports("services/market_snapshot.py"))
        self.assertNotIn("services.data_refresh", imports("services/evidence.py"))

    def test_session_executes_tools_through_one_dispatch_method(self):
        tree = ast.parse((ROOT / "agents/agent.py").read_text(encoding="utf-8"))
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentSession")
        methods = {node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)}
        self.assertIn("_execute_requested_tool", methods)
        self.assertIn("run", methods)
        dispatch_calls = [node for node in ast.walk(methods["run"]) if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Attribute) and node.func.attr == "_execute_requested_tool"]
        self.assertEqual(len(dispatch_calls), 1)
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "model_receipt_for_tool"
                            for node in ast.walk(methods["run"])))

    def test_model_receipt_exposes_only_approved_future_pair_fields(self):
        data = {"status": "NEEDS_INPUT", "message": "조건 확인", "missing_fields": ["가격"],
                "secret": "do not disclose", "money": 123456789}
        receipt = model_receipt_for_tool("compare_future_pair", data)
        self.assertNotIn("secret", receipt)
        self.assertNotIn("money", receipt)
        self.assertEqual(receipt["status"], "NEEDS_INPUT")
        self.assertEqual(receipt["missing_fields"], ["가격"])

    def test_chat_owns_report_request_matching(self):
        self.assertIn("_REPORT_REQUEST =", (ROOT / "ui/chat.py").read_text(encoding="utf-8"))
        for name in ("application", "context", "panels"):
            content = (ROOT / "ui" / (name + ".py")).read_text(encoding="utf-8")
            self.assertNotIn("_REPORT_REQUEST =", content)
            self.assertNotIn("_BASIS_CHOICES =", content)

if __name__ == "__main__":
    unittest.main()
