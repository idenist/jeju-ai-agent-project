"""세 Agent의 도구 결과를 손실 없이 공통 형식으로 변환하는지 검증합니다."""
from __future__ import annotations

from copy import deepcopy
import unittest

from agents.common.result_contract import (
    COMMON_STATUSES, JUNEOK_TOOLS, normalize_tool_result,
)


class ResultContractTests(unittest.TestCase):
    def test_current_juneok_tool_registry_matches_contract(self):
        from agents.juneok.tools import TOOLS
        self.assertEqual({item["name"] for item in TOOLS}, JUNEOK_TOOLS)

    def test_juneok_success_preserves_numbers_and_source(self):
        from agents.juneok.comparison import compare_shipment_options
        from agents.juneok.state import demo_scenario
        original = compare_shipment_options(demo_scenario())
        before = deepcopy(original)
        output = normalize_tool_result("compare_shipment_options", original)
        self.assertEqual(original, before)
        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["source_status"], "OK")
        self.assertEqual(output["data"], original)
        self.assertEqual(output["data"]["options"][0]["minimum_whole_krw_per_kg"], "3109")
        self.assertFalse(output["data"]["is_future_forecast"])
        output["data"]["options"][0]["minimum_whole_krw_per_kg"] = "0"
        self.assertEqual(original["options"][0]["minimum_whole_krw_per_kg"], "3109")

    def test_juneok_input_and_data_failures_remain_distinct(self):
        for status in ("NEEDS_INPUT", "NO_DATA", "INVALID_INPUT", "DATA_ERROR"):
            with self.subTest(status=status):
                output = normalize_tool_result("get_market_evidence", {
                    "status": status, "reason": "ORIGINAL_REASON", "missing_fields": ["grade"]})
                self.assertEqual(output["status"], status)
                self.assertEqual(output["reason"], "ORIGINAL_REASON")
                self.assertEqual(output["missing_fields"], ["grade"])

    def test_juneok_settlement_partial_or_check_required_is_not_ok(self):
        for status in ("PARTIAL", "CHECK_REQUIRED"):
            with self.subTest(status=status):
                output = normalize_tool_result("reconcile_settlement", {"status": status})
                self.assertEqual(output["status"], status)
                self.assertIn(status, COMMON_STATUSES)

    def test_juneok_needs_clarification_is_needs_input(self):
        output = normalize_tool_result("explore_data", {"status": "NEEDS_CLARIFICATION"})
        self.assertEqual(output["status"], "NEEDS_INPUT")
        self.assertEqual(output["source_status"], "NEEDS_CLARIFICATION")

    def test_jeonghyeon_ok_keeps_warning_counts_and_cautions(self):
        raw = {"status": "OK", "conditions": {"variety": "조생귤"}, "interval_days": 7,
               "clean_pairs": 2, "warning_pairs": 3, "unavailable_pairs": 7,
               "examples": [{"base_date": "2026-10-01", "target_date": "2026-10-08"}],
               "clean_change_range_pct": {"min": "-4", "max": "2"},
               "source": "data/processed/price_comparison_availability.csv",
               "cautions": ["미래가격 예측이 아닙니다."]}
        output = normalize_tool_result("get_price_history", raw)
        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["data"], raw)
        self.assertEqual(output["cautions"], ["미래가격 예측이 아닙니다."])
        self.assertTrue(any("3개" in x for x in output["warnings"]))
        self.assertEqual(output["source"], raw["source"])

    def test_jeonghyeon_no_clean_pairs_never_claim_success(self):
        output = normalize_tool_result("get_price_history", {
            "status": "NO_CLEAN_COMPARISON", "conditions": {"variety": "극조생감귤"},
            "interval_days": 7, "clean_pairs": 0, "warning_pairs": 2,
            "unavailable_pairs": 4, "examples": []})
        self.assertEqual(output["status"], "NO_DATA")
        self.assertEqual(output["source_status"], "NO_CLEAN_COMPARISON")
        self.assertEqual(output["reason"], "NO_CLEAN_COMPARISON")
        self.assertIn("경고", output["message"])
        self.assertEqual(output["data"]["warning_pairs"], 2)

    def test_jeonghyeon_unavailable_interval_retains_choices(self):
        raw = {"status": "INTERVAL_NOT_AVAILABLE", "available_intervals": [3, 7, 14]}
        output = normalize_tool_result("get_price_history", raw)
        self.assertEqual(output["status"], "NO_DATA")
        self.assertEqual(output["source_status"], "INTERVAL_NOT_AVAILABLE")
        self.assertEqual(output["data"]["available_intervals"], [3, 7, 14])

    def test_jeonghyeon_needs_clarification_preserves_choices(self):
        raw = {"status": "NEEDS_CLARIFICATION", "missing_fields": ["grade"],
               "choices": {"grade": ["특", "상"]}}
        output = normalize_tool_result("get_price_history", raw)
        self.assertEqual(output["status"], "NEEDS_INPUT")
        self.assertEqual(output["missing_fields"], ["grade"])
        self.assertEqual(output["data"]["choices"]["grade"], ["특", "상"])

    def test_jeonghyeon_ambiguous_codes_are_not_user_error(self):
        output = normalize_tool_result("get_price_history", {"status": "AMBIGUOUS_CODES"})
        self.assertEqual(output["status"], "DATA_ERROR")
        self.assertEqual(output["reason"], "AMBIGUOUS_CODES")

    def test_jeonghyeon_structured_error_kinds(self):
        for kind, expected in (("INVALID_INPUT", "INVALID_INPUT"), ("DATA_ERROR", "DATA_ERROR")):
            with self.subTest(kind=kind):
                output = normalize_tool_result("get_price_history", {
                    "status": "ERROR", "error_kind": kind, "error": "원래 오류 문구"})
                self.assertEqual(output["status"], expected)
                if kind == "INVALID_INPUT":
                    self.assertEqual(output["message"], "원래 오류 문구")
                else:
                    self.assertNotEqual(output["message"], "원래 오류 문구")
                self.assertEqual(output["source_status"], "ERROR")

    def test_seunghyuk_price_success_and_empty(self):
        success = {"has_price": True, "observed_date": "2026-11-03",
                   "observed_price_krw_per_kg": "2446.81",
                   "quality_flags": ["LOW_SAMPLE"]}
        result = normalize_tool_result("get_market_price", success)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["source_status"], "LEGACY_SUCCESS")
        self.assertTrue(any("LOW_SAMPLE" in x for x in result["warnings"]))
        empty = normalize_tool_result("get_market_price", {"has_price": False, "reason": "기록 없음"})
        self.assertEqual(empty["status"], "NO_DATA")
        self.assertEqual(empty["reason"], "기록 없음")

    def test_seunghyuk_compare_success_preserves_exact_decimal_text(self):
        raw = {"conditions": {"quantity_kg": "3000.00"},
               "break_even_price_krw_per_kg": "3108.8435374149",
               "current_price_krw_per_kg": "3000.00",
               "is_future_forecast": False}
        output = normalize_tool_result("compare_shipping", raw)
        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["data"]["break_even_price_krw_per_kg"], raw["break_even_price_krw_per_kg"])

    def test_seunghyuk_untyped_error_fails_closed(self):
        output = normalize_tool_result("compare_shipping", {"error": "C:\\사용자\\secret.csv를 읽지 못했습니다."})
        self.assertEqual(output["status"], "DATA_ERROR")
        self.assertEqual(output["reason"], "UNCLASSIFIED_LEGACY_ERROR")
        self.assertNotIn("secret.csv", output["message"])
        self.assertIn("secret.csv", output["data"]["error"])

    def test_malformed_result_cannot_be_success(self):
        for tool, raw in (("get_market_price", {"has_price": True}),
                          ("compare_shipping", {"conditions": {}}),
                          ("get_price_history", {}),
                          ("get_price_history", {"status": "OK"}),
                          ("get_market_evidence", {})):
            with self.subTest(tool=tool):
                output = normalize_tool_result(tool, raw)
                self.assertEqual(output["status"], "DATA_ERROR")
                self.assertEqual(output["reason"], "MALFORMED_RESULT")

    def test_unknown_status_or_tool_is_rejected(self):
        self.assertEqual(normalize_tool_result("get_price_history", {"status": "NEW_UNKNOWN"})["status"], "DATA_ERROR")
        self.assertEqual(normalize_tool_result("get_market_evidence", {"status": "NEW_UNKNOWN"})["status"], "DATA_ERROR")
        with self.assertRaises(ValueError):
            normalize_tool_result("unknown_tool", {"status": "OK"})
        with self.assertRaises(ValueError):
            normalize_tool_result("get_price_history", [])

    def test_missing_is_not_replaced_by_zero(self):
        original = {"status": "NEEDS_INPUT", "missing_fields": ["quantity_kg"],
                    "input": {"quantity_kg": None, "current_price_per_kg": 0}}
        output = normalize_tool_result("compare_shipment_options", original)
        self.assertIsNone(output["data"]["input"]["quantity_kg"])
        self.assertEqual(output["data"]["input"]["current_price_per_kg"], 0)


if __name__ == "__main__":
    unittest.main()
