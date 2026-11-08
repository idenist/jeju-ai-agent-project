import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from agents.juneok.agent import AgentSession
from agents.juneok.config import Settings
from agents.juneok.state import ScenarioStore, demo_scenario
from agents.juneok.tools import TOOLS, execute_tool


def call(name, args, number=1):
    return NS(type="function_call", name=name, arguments=json.dumps(args), call_id=f"call_{number}")


def response(*items, text="임의 금액 99999999원은 사용하지 마세요"):
    return NS(output=list(items), output_text=text, status="completed")


class FakeClient:
    def __init__(self, responses):
        self.responses = self
        self.items = iter(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        item = next(self.items)
        if isinstance(item, Exception):
            raise item
        return item


class HarnessTests(unittest.TestCase):
    def session(self, responses):
        session = AgentSession(Settings(model="mock-model"), FakeClient(responses))
        session.store = ScenarioStore(demo_scenario())
        return session

    def test_numeric_final_is_python_only(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 0})), response()])
        result = session.run("비교해 주세요")
        self.assertEqual(result["status"], "OK")
        self.assertIn("3,109", result["text"])
        self.assertNotIn("99999999", result["text"])

    def test_error_then_recovery_keeps_verified_numbers(self):
        session = self.session([response(call("unknown", {}, 1)),
                                response(call("compare_shipment_options", {"expected_version": 0}, 2)), response()])
        result = session.run("오류를 복구한 뒤 비교해 주세요")
        self.assertEqual(result["events"][0]["status"], "INVALID_INPUT")
        self.assertEqual(result["events"][1]["status"], "OK")
        self.assertIn("3,109", result["text"])
        self.assertNotIn("99999999", result["text"])

    def test_no_tool_cannot_publish_model_numbers(self):
        result = self.session([response(text="현재 99999999원입니다. 확인해 주세요")]).run("시세를 알려줘")
        self.assertNotIn("99999999", result["text"])

    def test_malformed_arguments_are_tool_error_not_crash(self):
        item = call("compare_shipment_options", {})
        item.arguments = "not-json"
        result = self.session([response(item), response()]).run("비교해줘")
        self.assertEqual(result["tool_results"][0]["result"]["status"], "INVALID_INPUT")

    def test_each_request_repeats_instructions_and_uses_stateless_api(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 0})), response()])
        session.run("계산")
        self.assertEqual(len(session.client.requests), 2)
        for req in session.client.requests:
            self.assertTrue(req["instructions"])
            self.assertFalse(req["store"])
            self.assertFalse(req["parallel_tool_calls"])
        out = [x for x in session.client.requests[1]["input"] if isinstance(x, dict) and x.get("type") == "function_call_output"]
        self.assertEqual(out[-1]["call_id"], "call_1")

    def test_period_change_then_reconfirmation_across_turns(self):
        options = demo_scenario()["options"]
        options[0].update(delay_days=3, loss_confirmed_for_period=False)
        confirmed = deepcopy(options)
        confirmed[0].update(loss_percent=1, loss_confirmed_for_period=True)
        session = self.session([
            response(call("update_scenario", {"expected_version": 0, "patch": {"options": options}})),
            response(call("compare_shipment_options", {"expected_version": 1}, 2)), response(),
            response(call("update_scenario", {"expected_version": 1, "patch": {"options": confirmed}}, 3)),
            response(call("compare_shipment_options", {"expected_version": 2}, 4)), response()])
        first = session.run("기간을 변경")
        self.assertEqual(first["tool_results"][1]["result"]["status"], "NEEDS_INPUT")
        second = session.run("새 기간 감모 확인")
        self.assertEqual(second["tool_results"][1]["result"]["status"], "OK")
        self.assertEqual(session.store.scenario["quantity_kg"], 3000)
        self.assertEqual(session.store.scenario["current_price_per_kg"], 3000)

    def test_request_limit_stops_loop(self):
        items = [response(call("compare_shipment_options", {"expected_version": 0}, i)) for i in range(6)]
        result = self.session(items).run("계속 호출")
        self.assertEqual(result["status"], "LIMIT_REACHED")
        self.assertEqual(len(result["events"]), 6)

    def test_api_error_is_sanitized_but_keeps_tool_result(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 0})), RuntimeError("PRIVATE_TOKEN_SHOULD_NOT_ESCAPE")])
        result = session.run("비교")
        self.assertEqual(result["status"], "API_ERROR")
        self.assertIn("3,109", result["text"])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        self.assertEqual(session.messages, [])

    def test_empty_key_does_not_make_network_request(self):
        result = AgentSession(Settings()).run("비교")
        self.assertEqual(result["status"], "KEY_REQUIRED")

    def test_unknown_arguments_and_stale_versions_are_rejected(self):
        store = ScenarioStore()
        self.assertEqual(execute_tool("compare_shipment_options", {"expected_version": 0, "unknown": 1}, store)["status"], "INVALID_INPUT")
        self.assertEqual(execute_tool("compare_shipment_options", {"expected_version": True}, store)["status"], "INVALID_INPUT")

    def test_confirmation_flag_is_consumed_not_reused(self):
        store = ScenarioStore(demo_scenario())
        options = deepcopy(store.scenario["options"])
        options[0]["delay_days"] = 3
        result = store.update({"options": options}, 0)
        self.assertEqual(result["loss_reconfirmation_required"], ["seven_days"])
        self.assertIsNone(store.scenario["options"][0]["loss_percent"])

    def test_compare_then_update_never_displays_stale_amount(self):
        session = self.session([
            response(call("compare_shipment_options", {"expected_version": 0})),
            response(call("update_scenario", {"expected_version": 0, "patch": {"current_price_per_kg": 4000}}, 2)),
            response()])
        result = session.run("단가 변경")
        self.assertIn("12,000,000", result["text"])
        self.assertNotIn("9,000,000", result["text"])
        self.assertEqual(result["tool_results"][-1]["result"]["scenario_version"], result["state"]["version"])

    def test_explicit_clear_retracts_confirmed_constraints(self):
        store = ScenarioStore(demo_scenario())
        store.update({"storage_available": True, "shipping_flexible": True}, 0)
        result = execute_tool("update_scenario", {"expected_version": 1, "patch": {},
                              "clear_fields": ["storage_available", "shipping_flexible"]}, store)
        self.assertEqual(result["status"], "OK")
        self.assertIsNone(store.scenario["storage_available"])
        self.assertIsNone(store.scenario["shipping_flexible"])

    def test_invalid_clear_rolls_back(self):
        store = ScenarioStore(demo_scenario())
        before = store.snapshot()
        result = store.update({"quantity_kg": 1}, 0, clear_fields=["quantity_kg"])
        self.assertEqual(result["status"], "INVALID_INPUT")
        self.assertEqual(store.snapshot(), before)

    def test_canonical_help_does_not_request_unrelated_quantity(self):
        session = self.session([response(text="수수료는 이미 차감한 것입니다.")])
        result = session.run("수수료 차감 후 단가의 뜻은 뭐야?")
        self.assertIn("수수료를 다시 차감하지 않습니다", result["text"])
        self.assertNotIn("필요한 항목", result["text"])

    def test_every_nested_object_is_strict(self):
        def check(schema):
            if schema.get("type") == "object":
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
            for child in schema.get("properties", {}).values():
                check(child)
            if "items" in schema:
                check(schema["items"])
        for schema in TOOLS:
            self.assertTrue(schema["strict"])
            check(schema["parameters"])


class ConfigTests(unittest.TestCase):
    def test_explicit_product_env_only_and_os_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "product"
            root.mkdir()
            (parent / ".env").write_text("OPENAI_API_KEY=PARENT_FAKE_KEY\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                self.assertFalse(Settings.load(root).api_key)
                (root / ".env").write_text("OPENAI_API_KEY=PRODUCT_FAKE_KEY\nOPENAI_MODEL=test-model\n", encoding="utf-8")
                loaded = Settings.load(root)
                self.assertEqual(loaded.api_key, "PRODUCT_FAKE_KEY")
                self.assertNotIn("PRODUCT_FAKE_KEY", repr(loaded))
                with patch.dict(os.environ, {"OPENAI_API_KEY": "OS_FAKE_KEY"}):
                    self.assertEqual(Settings.load(root).api_key, "OS_FAKE_KEY")


class EvaluationTests(unittest.TestCase):
    def test_evidence_preflight_detects_wrong_variety_fixture(self):
        from scripts.evaluate_juneok import preflight
        case = {"id": "J08", "expected_tool": "get_historical_shipment_context", "expected_status": "OK", "partial_month": True,
                "evidence_args": {"shipment_variety": "노지온주", "target_month": 9, "start_year": 2025, "end_year": 2025}}
        self.assertFalse(preflight([case])[0]["passed"])

    def test_all_current_evidence_fixtures_have_real_data(self):
        from scripts.evaluate_juneok import preflight
        from agents.juneok.config import PROJECT_ROOT
        cases = json.loads((PROJECT_ROOT / "evals/juneok_cases.json").read_text(encoding="utf-8"))
        checks = preflight(cases)
        self.assertEqual(len(checks), 6)
        self.assertTrue(all(x["passed"] for x in checks))

    def test_missing_hypothesis_fails_evaluation_without_crashing(self):
        from scripts.evaluate_juneok import score
        from agents.juneok.comparison import compare_shipment_options
        case = {"id": "test", "expected_tool": "compare_shipment_options", "expected_status": "OK", "difference_krw": "562000"}
        turns = [{"status": "OK", "tool_results": [{"tool": "compare_shipment_options", "result": compare_shipment_options(demo_scenario())}]}]
        scored = score(case, turns)
        self.assertFalse(scored["passed"])
        self.assertIn("wrong_hypothetical_difference", scored["failures"])

    def test_api_failure_is_a_failure_not_a_skipped_case(self):
        from scripts.evaluate_juneok import score
        result = score({"id": "test", "forbid_successful_comparison": True}, [{"status": "API_ERROR", "tool_results": []}])
        self.assertFalse(result["passed"])
        self.assertIn("model_or_api_error", result["failures"])


if __name__ == "__main__":
    unittest.main()
