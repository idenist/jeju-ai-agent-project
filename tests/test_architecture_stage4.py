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

    def test_model_receipt_exposes_only_approved_future_pair_fields(self):
        data = {"status": "NEEDS_INPUT", "message": "조건 확인", "missing_fields": ["가격"],
                "secret": "do not disclose", "money": 123456789}
        receipt = model_receipt_for_tool("compare_future_pair", data)
        self.assertNotIn("secret", receipt)
        self.assertNotIn("money", receipt)
        self.assertEqual(receipt["status"], "NEEDS_INPUT")
        self.assertEqual(receipt["missing_fields"], ["가격"])

if __name__ == "__main__":
    unittest.main()
