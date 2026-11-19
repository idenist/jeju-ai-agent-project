"""Regression checks for constraints and explicit uncertainty in a P2 session."""
from datetime import date
from unittest import TestCase
from unittest.mock import patch

from agents.agent import AgentSession
from agents.future_pair_intent import shipment_availability, stated_storage_range
from core.config import Settings
from presenters.market_export import export_market_record
from presenters.reporting import render_result
from services.shipment_timing import resolve_pair_dates


TODAY = date(2026, 11, 19)
INPUTS = {
    "quantity": 3000, "start_date": "2026-11-20", "price_basis": "gross",
    "price_a": 2500, "price_b": 2640,
    "option_a": {"delay_days": 3, "loss_percent": 1, "fee_percent": 5,
                 "daily_storage_cost": 10000, "other_cost_krw": 5000},
    "option_b": {"delay_days": 10, "loss_percent": 4, "fee_percent": 7,
                 "daily_storage_cost": 5000, "other_cost_krw": 5000},
}


class FuturePairAvailabilityTests(TestCase):
    def setUp(self):
        self.agent = AgentSession(settings=Settings(api_key="unused"))
        self.state = self.agent.future_pair

    def test_shipment_window_with_an_exception(self):
        with patch("services.data_refresh.kst_today", return_value=TODAY):
            first = self.agent.run("11월 23~30일에 낼 수 있는데 30일은 인수가 안 돼요")
            self.assertIn("30일은 비교에서 뺄게요", first["text"])
            self.assertEqual(first["text"].count("?"), 1)
            blocked = self.agent.run("30일로 해줘")
            self.assertIn("30일은 출하 가능한 날에서 뺐어요", blocked["text"])
            self.assertEqual(resolve_pair_dates(self.state.inputs), (None, None))
            self.agent.run("28일로 해줘")
        self.assertEqual(resolve_pair_dates(self.state.inputs), ("2026-11-23", "2026-11-28"))

    def test_unavailable_b_invalidates_result_without_changing_a(self):
        self.assertEqual(self.state.apply("new", 0, INPUTS)["status"], "OK")
        a = self.state.snapshot()["inputs"]["option_a"]
        with patch("services.data_refresh.kst_today", return_value=TODAY):
            self.agent.run("11월 30일은 인수가 안 돼요.")
            self.assertIsNone(self.state.current_result())
            self.assertIsNone(self.state.inputs["price_b"])
            self.agent.run("B안만 11월 28일로 바꿔줘.")
        self.assertEqual(self.state.inputs["option_a"], a)
        self.assertEqual(self.state.inputs["price_a"], 2500)
        self.assertEqual(self.state.inputs["quantity"], 3000)
        self.assertEqual(resolve_pair_dates(self.state.inputs), ("2026-11-23", "2026-11-28"))

    def test_storage_range_and_total_followup(self):
        self.state.apply("new", 0, INPUTS)
        reply = self.agent.run("열흘 보관비는 0~10만 원 사이예요.")
        result = reply["visible_results"][0]["result"]
        bounds = result["storage_cost_range"]
        self.assertEqual(bounds["minimum_from_krw_per_kg"], "2623")
        self.assertEqual(bounds["minimum_to_krw_per_kg"], "2660")
        self.assertEqual(bounds["storage_cost_at_tie_krw"], "47226.00")
        self.assertIsNone(result["minimum_whole_krw_per_kg"])
        self.assertIsNone(self.state.inputs["option_b"]["daily_storage_cost"])
        self.assertIn("2,623~2,660원/kg", render_result(result))
        self.assertIn("47,226원", reply["text"])
        self.assertEqual(reply["text"].count("?"), 1)
        self.assertEqual(export_market_record(result, None)["status"], "NOT_REQUESTED")
        confirmed = self.agent.run("4만 원이에요")["visible_results"][0]["result"]
        self.assertEqual(confirmed["storage_cost_range"]["storage_cost_total_to_krw"], "40000")
        self.assertEqual(confirmed["storage_cost_range"]["assessment"], "B_HIGHER")

    def test_daily_range_uses_current_b_days_and_other_unknowns_stay_unknown(self):
        self.state.apply("new", 0, INPUTS)
        self.state.apply("edit", self.state.version, {}, ["option_a.other_cost_krw"])
        result = self.agent.run("B안 하루 보관비는 5천원~1만원")["visible_results"][0]["result"]
        bounds = result["storage_cost_range"]
        self.assertEqual(bounds["storage_cost_total_from_krw"], "50000")
        self.assertEqual(bounds["assessment"], "OTHER_COSTS_UNKNOWN")
        self.assertIsNone(bounds["storage_cost_at_tie_krw"])
        self.assertIn("A안 별도 비용", result["unknown_cost_fields"])

    def test_combined_range_replaces_model_supplied_endpoint_and_expires_on_date_edit(self):
        combined = "열흘 보관비는 최소 0원에서 최대 10만 원 사이이고, 손익 비교해 줘."
        supplied = stated_storage_range(combined)
        self.assertIsNotNone(supplied)
        result = self.state.apply("new", 0, INPUTS, storage_range=supplied)
        self.assertIsNone(self.state.inputs["option_b"]["daily_storage_cost"])
        self.assertEqual(result["storage_cost_range"]["storage_cost_total_to_krw"], "100000")
        self.state.apply("edit", self.state.version, {"option_b": {"delay_days": 17}})
        self.assertIsNone(self.state.storage_cost_range)
        self.assertIsNone(self.state.current_result())

    def test_unconfirmed_date_does_not_remove_candidate(self):
        self.assertEqual(shipment_availability("30일은 인수가 안 돼요?", TODAY)[1], set())
        self.state.apply("new", 0, INPUTS)
        self.assertIsNone(self.state.follow_up_availability("11월 30일은 인수가 안 되나요?", TODAY))
        self.assertIsNotNone(self.state.current_result())

    def test_followup_quote_is_kept_with_existing_cost_range(self):
        self.state.apply("new", 0, {**INPUTS, "price_b": None})
        self.agent.run("열흘 보관비는 0원에서 10만 원 사이예요.")
        reply = self.agent.run("2640원")
        result = reply["visible_results"][0]["result"]
        self.assertEqual(result["storage_cost_range"]["quote_krw_per_kg"], "2640")
        self.assertEqual(result["storage_cost_range"]["assessment"], "DEPENDS_ON_STORAGE")
        self.assertIsNone(self.state.inputs["option_b"]["daily_storage_cost"])
