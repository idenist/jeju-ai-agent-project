"""Regression checks for the A/B + market PDF/JSON export record."""
import json
import unittest

from ui.market_export import (
    export_json, export_market_record, market_markdown, report_entries,
)

PAIR = {"analysis_type": "future_pair", "status": "OK", "price_basis": "gross"}
MARKET = {
    "analysis_type": "market_decision", "status": "OK",
    "comparison": {"option_b_date": "2026-11-25", "minimum_whole_krw_per_kg": "2660",
                   "difference_vs_a_krw": "241850", "decision_under_assumptions": "B_ASSUMED_HIGHER"},
    "market_evidence": {"conditions": {"market_code": "110001", "variety": "조생귤"},
                        "seasons": [
                            {"season": "2023-2024", "status": "OK", "days_clean": 7, "days_above": 4,
                             "daily_share_percent": "57.1", "kg_share_percent": "58.2", "window_complete": True},
                            {"season": "2026-2027", "status": "NO_DATA", "days_clean": 0, "days_above": 0,
                             "daily_share_percent": None, "kg_share_percent": None}]}}


class MarketExportReportTests(unittest.TestCase):
    def test_market_value_matches_report(self):
        content = market_markdown(MARKET)
        self.assertIn("2,660원/kg", content)
        self.assertIn("241,850원", content)
        self.assertIn("4 | 57.1% | 58.2%", content)
        self.assertIn("미래 가격 달성 확률", content)

    def test_report_and_json_use_same_result(self):
        entries = report_entries(PAIR, MARKET)
        payload = json.loads(export_json(PAIR, MARKET))
        self.assertEqual(entries[1]["result"], payload["market_decision"])
        self.assertEqual(entries[0]["result"], payload["result"])

    def test_existing_chat_metadata_preserved(self):
        payload = json.loads(export_json(PAIR, MARKET, '{"session_id":"original"}'))
        self.assertEqual(payload["session_id"], "original")
        self.assertEqual(payload["market_decision"], MARKET)

    def test_no_spec_is_not_presented_as_observed(self):
        result = export_market_record(PAIR, None)
        self.assertEqual(result["status"], "NEEDS_SPEC")
        self.assertNotIn("100.0%", market_markdown(result))

    def test_net_basis_is_not_compared_to_gross(self):
        result = export_market_record({**PAIR, "price_basis": "commission_net"}, None)
        self.assertEqual(result["status"], "NOT_COMPARABLE")


if __name__ == "__main__":
    unittest.main()
