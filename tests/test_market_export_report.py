"""Regression checks for the A/B + market PDF/JSON export record."""
import json
import unittest

from presenters.market_export import (
    export_json, export_market_record, market_markdown, report_entries, season_observation_rows,
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
        self.assertIn("4 / 7일", content)
        self.assertIn("미래에 그 가격을 받을 확률", content)

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

    def test_partial_missing_and_actual_zero_share_one_display_contract(self):
        conditions = {"window_days": 7}
        evidence = {"conditions": conditions, "seasons": [
            {"season": "2023-2024", "status": "OK", "days_above": 0, "days_clean": 7, "window_complete": True},
            {"season": "2024-2025", "status": "OK", "days_above": 1, "days_clean": 2, "window_complete": False},
            {"season": "2025-2026", "status": "NO_CLEAN_DAYS", "days_above": 0, "days_clean": 0},
            {"season": "2026-2027", "status": "NO_DATA", "days_above": 0, "days_clean": 0,
             "source_first_date": "2026-10-01", "source_last_date": "2026-11-13",
             "window_start": "2026-11-18", "window_end": "2026-12-02"},
        ]}
        market = {**MARKET, "market_evidence": evidence}
        markdown = market_markdown(market)
        rows = list(season_observation_rows(evidence))
        self.assertEqual(rows, [
            ("2023-2024", "0 / 7일", "조회됨"),
            ("2024-2025", "1 / 2일", "부분 관측기간"),
            ("2025-2026", "-", "품질 경고로 비교 불가"),
            ("2026-2027", "-", "해당 기간 관측 없음"),
        ])
        for row in rows:
            self.assertIn(" | ".join(row), markdown)
        self.assertIn("B안 출하일 전후 7일", markdown)
        exported = json.loads(export_json(PAIR, market))["market_decision"]["market_evidence"]
        self.assertEqual(exported, evidence)

    def test_export_rejects_saved_analysis_for_another_product_date_or_price_basis(self):
        spec = {"market_code": "110001", "variety": "조생귤", "grade": "특",
                "size": "소", "package": "상자", "package_kg": "5", "trade_type": "경매"}
        pair = {**PAIR, "minimum_whole_krw_per_kg": "2660",
                "options_b": [{"id": "past_mid", "sale_date": "2026-11-25"}],
                "evidence": {"past_mid": {"conditions": spec}}}
        market = {**MARKET, "market_evidence": {**MARKET["market_evidence"], "conditions": spec}}
        stored = {"result": market}
        self.assertEqual(export_market_record(pair, stored), market)
        for edit in (
            {"evidence": {"past_mid": {"conditions": {**spec, "variety": "하우스감귤"}}}},
            {"options_b": [{"id": "past_mid", "sale_date": "2026-12-02"}]},
            {"minimum_whole_krw_per_kg": "2750"},
        ):
            self.assertEqual(export_market_record({**pair, **edit}, stored)["status"], "NEEDS_SPEC")
        self.assertEqual(export_market_record({**pair, "price_basis": "commission_net"}, stored)["status"],
                         "NOT_COMPARABLE")


if __name__ == "__main__":
    unittest.main()
