"""A/B price gaps use a single verified reason and recovery action."""

import unittest
from contextlib import nullcontext
from io import BytesIO
from unittest.mock import patch

from pypdf import PdfReader

from agents.future_pair_state import FuturePairState
from presenters.presentation import render_dialogue
from presenters.reporting import render_report
from ui.pdf_report import render_pdf_report


FILTERS = {
    "market_code": "110001", "variety": "조생귤", "grade": "특",
    "size": "소", "package": "상자", "package_kg": "5", "trade_type": "경매",
}
CONDITIONS = {
    "quantity": 3000, "price_basis": "gross", "start_date": "2026-11-20", "price_a": 2500,
    "option_a": {"delay_days": 3, "loss_percent": 1, "daily_storage_cost": 10000,
                 "other_cost_krw": 5000, "fee_percent": 5},
    "option_b": {"delay_days": 10, "loss_percent": 4, "daily_storage_cost": 10000,
                 "other_cost_krw": 5000, "fee_percent": 7},
}
SCENARIOS = (
    ({**FILTERS, "package_kg": "9"}, "NO_DATA", "NO_OBSERVATION", False),
    ({**FILTERS, "variety": "비가림감귤", "size": "특대"}, "NO_CLEAN_COMPARISON", "NO_OBSERVATION", False),
    ({**FILTERS, "size": "미상"}, "NO_CLEAN_COMPARISON", "QUALITY_EXCLUDED", False),
    (FILTERS, "DATA_ERROR", "FILE_UNVERIFIED", True),
)


def _price_source(failed):
    if not failed:
        return nullcontext()
    return patch("services.future_pair.verify_cached_price_sources",
                 return_value={"status": "DATA_ERROR", "full_content_verified": False})


class FuturePairDataGapTests(unittest.TestCase):
    def _assert_reason_and_action(self, result, source_status, code, *, check_pdf=False):
        self.assertEqual(result["source_status"], source_status)
        gap = result["data_gap"]
        self.assertEqual(gap["code"], code)
        self.assertTrue(gap["reason"].strip())
        self.assertTrue(gap["next_action"].strip())
        entries = [{"tool": "compare_future_pair", "result": result}]
        outputs = [render_dialogue(entries), render_report(entries)]
        if check_pdf:
            pdf = PdfReader(BytesIO(render_pdf_report(entries)))
            outputs.append("\n".join(page.extract_text() for page in pdf.pages))
        for text in outputs:
            display = " ".join(text.split())
            self.assertIn(gap["reason"], display)
            self.assertEqual(display.count(gap["next_action"]), 1)
            self.assertNotIn("241,850", display)
        self.assertTrue(result["manual_price_required"])
        self.assertNotIn("options_b", result)

    def test_completed_inputs_distinguish_gaps_and_replace_the_previous_result(self):
        with_reasons = []
        for filters, source_status, code, source_fails in SCENARIOS:
            with self.subTest(filters=filters, code=code):
                pair = FuturePairState()
                initial = pair.apply("new", pair.version, {
                    **CONDITIONS, "price_mode": "manual", "price_b": 2750,
                })
                self.assertEqual(initial["status"], "OK")
                self.assertIsNotNone(pair.current_result())
                with _price_source(source_fails):
                    gap = pair.apply("edit", pair.version, {"price_mode": "history", "history_filters": filters})
                self._assert_reason_and_action(gap, source_status, code, check_pdf=True)
                self.assertIsNone(pair.current_result())
                self.assertIsNone(pair.inputs["price_b"])
                self.assertEqual(pair.inputs["price_a"], 2500)
                self.assertEqual(pair.inputs["option_a"]["delay_days"], 3)
                self.assertEqual(pair.inputs["option_b"]["delay_days"], 10)
                with_reasons.append(gap["data_gap"]["reason"])
        self.assertGreaterEqual(len(set(with_reasons)), 3)

    def test_price_preview_without_costs_uses_the_same_gap_contract(self):
        for filters, source_status, code, source_fails in SCENARIOS:
            with self.subTest(filters=filters, code=code):
                pair = FuturePairState()
                with _price_source(source_fails):
                    gap = pair.apply("new", pair.version, {
                        **CONDITIONS, "price_mode": "history", "history_filters": filters,
                        "option_a": {"delay_days": 3}, "option_b": {"delay_days": 10},
                    })
                self._assert_reason_and_action(gap, source_status, code)
                self.assertIsNone(pair.current_result())
                self.assertIsNone(pair.inputs["price_b"])
                self.assertIsNone(pair.inputs["option_b"]["daily_storage_cost"])
