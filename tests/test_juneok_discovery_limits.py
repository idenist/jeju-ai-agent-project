from copy import deepcopy
from decimal import Decimal
from io import BytesIO
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from agents.juneok.catalog import explore_data
from agents.juneok.limits import solve_offer_limits
from agents.juneok.state import demo_scenario, ScenarioStore
from agents.juneok.tools import execute_tool
from agents.juneok.agent import AgentSession
from agents.juneok.config import Settings
from agents.juneok.workspace import SharedWorkspace
from agents.juneok.reporting import render_result
from test_juneok_harness import FakeClient, call, response
import test_juneok_evidence as evidence_fixtures


class DiscoveryTests(unittest.TestCase):
    def test_actual_joint_conditions_and_numeric_filter(self):
        result = explore_data("market", {"variety": "조생귤", "package_kg": "5.000"})
        self.assertEqual(result["status"], "OK")
        self.assertTrue(all(c["conditions"]["variety"] == "조생귤" and Decimal(c["conditions"]["package_kg"]) == 5 for c in result["candidates"]))
        self.assertNotIn("current_price_per_kg", str(result))

    def test_pagination_is_disjoint_and_bounded(self):
        first = explore_data("market"); second = explore_data("market", offset=20)
        self.assertEqual(len(first["candidates"]), 20)
        self.assertEqual(first["next_offset"], 20)
        self.assertFalse({str(x["conditions"]) for x in first["candidates"]} & {str(x["conditions"]) for x in second["candidates"]})

    def test_nonexistent_and_wrong_source_filter(self):
        self.assertEqual(explore_data("market", {"variety": "존재하지않음"})["status"], "NO_DATA")
        for source, filters in [("market", {"year": 2025}), ("shipment", {"variety": "조생귤"}), ("bad", {})]:
            self.assertEqual(explore_data(source, filters)["status"], "INVALID_INPUT")

    def test_invalid_numbers_and_page(self):
        for value in [True, "NaN", 0, -1]:
            self.assertEqual(explore_data("market", {"package_kg": value})["status"], "INVALID_INPUT")
        self.assertEqual(explore_data("shipment", {"year": True})["status"], "INVALID_INPUT")
        self.assertEqual(explore_data("market", limit=21)["status"], "INVALID_INPUT")

    def test_weight_in_package_is_recoverable_error_not_false_no_data(self):
        wrong = explore_data("market", {"package": "5kg"})
        self.assertEqual(wrong["status"], "INVALID_INPUT")
        self.assertIn("package_kg=5", wrong["message"])
        self.assertEqual(explore_data("market", {"package": "상자", "package_kg": 5})["status"], "OK")
        session = AgentSession(Settings(model="mock"), FakeClient([
            response(call("explore_data", {"source": "market", "filters": {"package": "5kg"}, "finish_turn": True})),
            response(call("explore_data", {"source": "market", "filters": {"package": "상자", "package_kg": 5}, "finish_turn": True}))]))
        turn = session.run("5kg 상자 규격 탐색")
        self.assertEqual(turn["status"], "OK")
        self.assertEqual([e["status"] for e in turn["events"]], ["INVALID_INPUT", "OK"])

    def test_shipment_metadata_and_partial_month(self):
        result = explore_data("shipment", {"year": 2025, "target_month": 9})
        self.assertEqual(result["status"], "OK")
        self.assertIn("PARTIAL_MONTH", result["candidates"][0]["warnings"])
        self.assertEqual(explore_data("shipment", {"year": 2025, "target_month": 11})["status"], "NO_DATA")

    def test_ambiguous_codes_and_quality_are_not_hidden(self):
        fixture = evidence_fixtures.EvidenceTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        rows = fixture.market_rows
        extra = deepcopy(rows[0]); extra["variety_code"] = "different"; rows.append(extra)
        evidence_fixtures.write_csv(fixture.root, "data/processed/daily_prices.csv", rows)
        result = explore_data("market", root=fixture.root)
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["candidates"][0]["ambiguous_codes"])
        self.assertIn("LOW_SAMPLE", result["candidates"][0]["warnings"])

    def test_missing_file_is_data_error(self):
        with patch("agents.juneok.catalog._market_rows", side_effect=FileNotFoundError):
            self.assertEqual(explore_data("market")["status"], "DATA_ERROR")

    def test_fake_model_discovery_preserves_state_and_selection(self):
        session = AgentSession(Settings(model="mock"), FakeClient([response(call("explore_data", {"source": "market", "filters": {"variety": "조생귤"}, "offset": 0, "limit": 5, "finish_turn": True}))]))
        before = session.store.snapshot(); result = session.run("조생귤 자료 찾아줘")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(before, session.store.snapshot())
        self.assertEqual(session.task_context.snapshot()["market_query"], {"variety": "조생귤"})
        self.assertIn("실제 선택 후보", result["text"])


class OfferLimitsTests(unittest.TestCase):
    def test_live_state_check_rejects_hypothesis_saved_as_condition(self):
        from scripts.verify_juneok_product_expansion_live import economic_inputs
        before = ScenarioStore(demo_scenario()).snapshot(); after = deepcopy(before)
        after["version"] += 1; after["scenario"]["options"][0]["loss_confirmed_for_period"] = False
        self.assertEqual(economic_inputs(before), economic_inputs(after))
        after["scenario"]["options"][0]["future_price_per_kg"] = 3300
        self.assertNotEqual(economic_inputs(before), economic_inputs(after))

    def solve(self, target="loss_percent", quote=3300, scenario=None):
        return solve_offer_limits(scenario or demo_scenario(), None, quote, target)

    def test_loss_boundary_and_state_unchanged(self):
        scenario = demo_scenario(); before = deepcopy(scenario); result = self.solve(scenario=scenario)
        self.assertEqual(result["conservative_limit"], "7.67")
        self.assertAlmostEqual(float(result["exact_boundary"]), 7.67676767)
        self.assertEqual(scenario, before)

    def test_cost_boundary_floors_whole_won(self):
        result = self.solve("daily_storage_cost")
        self.assertEqual(result["conservative_limit"], "100285")
        self.assertTrue(Decimal(result["exact_boundary"]) > 100285)

    def test_report_uses_this_offer_not_different_saved_future_price(self):
        s = demo_scenario(); s["options"][0]["future_price_per_kg"] = 3700
        rendered = render_result(self.solve(scenario=s))
        self.assertIn("3,300", rendered); self.assertNotIn("3,700", rendered)
        self.assertEqual(s["options"][0]["future_price_per_kg"], 3700)

    def test_independent_forward_checks_both_sides(self):
        from agents.juneok.comparison import compare_shipment_options
        for field, values in [("loss_percent", ["7.67", "7.68"]), ("daily_storage_cost", [100285, 100286])]:
            differences = []
            for value in values:
                s = demo_scenario(); s["options"][0][field] = value; s["options"][0]["future_price_per_kg"] = 3300
                differences.append(Decimal(compare_shipment_options(s)["options"][0]["difference_krw"]))
            self.assertGreaterEqual(differences[0], 0); self.assertLess(differences[1], 0)

    def test_unknown_solved_variable_allowed_other_unknown_asked(self):
        for target in ["loss_percent", "daily_storage_cost"]:
            s = demo_scenario(); s["options"][0][target] = None
            self.assertEqual(self.solve(target, scenario=s)["status"], "OK")
        s = demo_scenario(); s["options"][0]["daily_storage_cost"] = None
        self.assertEqual(self.solve(scenario=s)["status"], "NEEDS_INPUT")
        self.assertIsNone(s["options"][0]["daily_storage_cost"])

    def test_zero_quote_and_no_nonnegative_solution(self):
        for target in ["loss_percent", "daily_storage_cost"]:
            self.assertEqual(self.solve(target, 0)["boundary_kind"], "NO_FEASIBLE_VALUE")
        self.assertEqual(self.solve(quote=3000)["boundary_kind"], "NO_FEASIBLE_VALUE")

    def test_exact_zero_boundary(self):
        s = demo_scenario(); s["options"][0]["daily_storage_cost"] = 0
        self.assertEqual(Decimal(self.solve(quote=3000, scenario=s)["conservative_limit"]), 0)

    def test_negative_current_proceeds_all_domain(self):
        s = demo_scenario(); s["cost_policy"] = "itemized"; s["current_costs"] = [{"category": "other", "amount_krw": 10000000}]; s["options"][0]["costs"] = []
        self.assertEqual(self.solve(quote=0, scenario=s)["boundary_kind"], "ALL_VALID_VALUES")

    def test_gross_fees_and_itemized_independent_equation(self):
        s = demo_scenario(); s.update(price_basis="gross", included_costs=[], current_fee_percent=10, cost_policy="itemized", current_costs=[{"category": "transport", "amount_krw": 20000}])
        s["options"][0].update(fee_percent=5, costs=[{"category": "transport", "amount_krw": 50000}])
        expected = (1 - Decimal(3000*3000*.9 - 20000 + 140000 + 50000) / (Decimal(3000)*3300*Decimal('.95'))) * 100
        self.assertEqual(Decimal(self.solve(scenario=s)["exact_boundary"]), expected)

    def test_storage_double_count_rejected(self):
        s = demo_scenario(); s["included_costs"].append("storage")
        self.assertEqual(self.solve("daily_storage_cost", scenario=s)["status"], "INVALID_INPUT")
        s = demo_scenario(); s.update(cost_policy="itemized", current_costs=[]); s["options"][0]["costs"] = [{"category": "storage", "amount_krw": 2000}]
        self.assertEqual(self.solve("daily_storage_cost", scenario=s)["status"], "INVALID_INPUT")

    def test_missing_quote_invalid_and_stale_version(self):
        self.assertEqual(self.solve(quote=None)["missing_fields"], ["quoted_price_per_kg"])
        for quote in [True, -1, "NaN", "Infinity"]: self.assertEqual(self.solve(quote=quote)["status"], "INVALID_INPUT")
        self.assertEqual(execute_tool("solve_offer_limits", {"expected_version": 1}, ScenarioStore(demo_scenario()))["status"], "INVALID_INPUT")

    def test_only_selected_option_needs_complete_inputs(self):
        s = demo_scenario(); s["options"].append({"id": "unknown", "delay_days": 3})
        self.assertEqual(solve_offer_limits(s, "seven_days", 3300, "loss_percent")["status"], "OK")
        self.assertEqual(solve_offer_limits(s, None, 3300, "loss_percent")["status"], "NEEDS_INPUT")

    def test_execution_constraint_preserved(self):
        s = demo_scenario(); s["storage_available"] = False
        self.assertEqual(self.solve(scenario=s)["feasibility"]["status"], "CONFLICT")

    def test_shared_workspace_pdf_and_context_compatibility(self):
        from pypdf import PdfReader
        from agents.juneok.pdf_report import render_pdf_report
        from agents.juneok.casefiles import parse_case
        ws = SharedWorkspace(AgentSession()); ws.load_demo(); before = ws.store.snapshot()
        ws.offer_limits(None, 3300, "loss_percent")
        text = "".join(p.extract_text() for p in PdfReader(BytesIO(render_pdf_report(ws.current_entries()))).pages)
        self.assertIn("7.67", text); self.assertEqual(before, ws.store.snapshot())
        self.assertNotIn("| 조건", text)
        self.assertEqual(parse_case(ws.export_case("역산"))["snapshot"], before)

    def test_update_then_inverse_does_not_ask_for_solved_unknown(self):
        s = demo_scenario(); s["options"][0]["loss_percent"] = None
        session = AgentSession(Settings(model="mock"), FakeClient([
            response(call("update_scenario", {"expected_version": 0, "patch": s, "clear_fields": []})),
            response(call("solve_offer_limits", {"expected_version": 1, "option_id": "seven_days", "quoted_price_per_kg": 3300, "solve_for": "loss_percent", "finish_turn": True}))]))
        turn = session.run("조건을 저장한 뒤 감모 상한을 역산")
        self.assertEqual(turn["status"], "OK")
        self.assertEqual([x["tool"] for x in turn["visible_results"]], ["solve_offer_limits"])
        self.assertIsNone(session.store.scenario["options"][0]["loss_percent"])


class DiscoveryLimitsUiTests(unittest.TestCase):
    def test_keyless_offer_and_discovery_selection(self):
        from streamlit.testing.v1 import AppTest
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=35).run()
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            app.text_input(key="limits_quote").set_value("3300").run()
            next(b for b in app.button if b.label == "제안 단가의 허용 조건 계산").click().run()
            self.assertTrue(any("7.67" in str(m.value) for m in app.markdown))
            before = app.session_state.workspace.store.snapshot()
            next(b for b in app.button if b.label == "실제 자료 후보 탐색").click().run()
            self.assertEqual(app.session_state.workspace.entries[0]["result"]["analysis_type"], "data_catalog")
            self.assertEqual(before, app.session_state.workspace.store.snapshot())
            candidates = app.session_state.discovery_result["result"]["candidates"]
            chosen = next(i for i, c in enumerate(candidates) if not c.get("ambiguous_codes"))
            app.selectbox(key="discovery_candidate").set_value(chosen).run()
            next(b for b in app.button if b.label == "선택 후보의 관측자료 조회").click().run()
            self.assertEqual(app.session_state.workspace.entries[0]["tool"], "get_market_evidence")
            self.assertEqual(before, app.session_state.workspace.store.snapshot())
            grade = next(x for x in app.selectbox(key="discovery_market_grade").options if x != "선택 안 함")
            app.selectbox(key="discovery_market_grade").set_value(grade).run()
            varieties = [v for v in app.selectbox(key="discovery_market_variety").options if v != "선택 안 함"]
            app.selectbox(key="discovery_market_variety").set_value(varieties[-1]).run()
            self.assertIsNone(app.session_state.discovery_market_grade)
            self.assertFalse(list(app.exception))
