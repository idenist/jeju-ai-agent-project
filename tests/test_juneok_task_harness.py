"""Failure, no-progress and bounded task-context acceptance checks."""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx
from openai import APITimeoutError, APIConnectionError, AuthenticationError, RateLimitError

from agents.juneok.agent import AgentSession
from agents.juneok.config import Settings
from agents.juneok.harness import ExecutionPolicy, RepeatGuard, RunStopped, encoded_size, user_starts
from agents.juneok.task_context import TaskContext
from agents.juneok.workspace import SharedWorkspace
from agents.juneok.state import ScenarioStore, demo_scenario
from agents.juneok.casefiles import parse_case
from agents.juneok.reporting import render_report
from test_juneok_harness import FakeClient, call, response


class TaskHarnessTests(unittest.TestCase):
    def session(self, items, **policy):
        session = AgentSession(Settings(api_key="", model="fake"), FakeClient(items), replace(ExecutionPolicy(), **policy))
        session.store = ScenarioStore(demo_scenario())
        return session

    def workspace(self):
        ws = SharedWorkspace(AgentSession(Settings(api_key="")))
        ws.load_demo()
        return ws

    def query(self, year=2024):
        return {"shipment_variety": "노지온주", "target_month": 11, "start_year": year, "end_year": year}

    def rehash(self, document):
        raw = json.dumps(document["payload"], sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        document["sha256"] = hashlib.sha256(raw).hexdigest()
        return json.dumps(document, ensure_ascii=False).encode()

    def test_sdk_failures_have_distinct_sanitized_reasons(self):
        request = httpx.Request("POST", "https://example.invalid/responses")
        cases = [(APITimeoutError(request=request), "API_TIMEOUT"),
                 (APIConnectionError(request=request), "CONNECTION_ERROR"),
                 (AuthenticationError("PRIVATE_TOKEN", response=httpx.Response(401, request=request), body=None), "AUTH_ERROR"),
                 (RateLimitError("PRIVATE_TOKEN", response=httpx.Response(429, request=request), body=None), "RATE_LIMITED")]
        for error, expected in cases:
            with self.subTest(reason=expected):
                session = self.session([error]); before = session.store.snapshot()
                result = session.run("비교해주세요")
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["termination"]["phase"], "model_request")
                self.assertEqual(before, session.store.snapshot())
                self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
                self.assertEqual(session.messages, [])

    def test_internal_tool_failure_is_not_provider_failure(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 0}, 1)),
                                response(call("get_historical_shipment_context", self.query(), 2))])
        from agents.juneok.tools import execute_tool as real
        def invoke(name, args, store):
            if name == "get_historical_shipment_context": raise RuntimeError("PRIVATE_TOKEN")
            return real(name, args, store)
        with patch("agents.juneok.agent.execute_tool", side_effect=invoke): result = session.run("비교와 조회")
        self.assertEqual(result["status"], "INTERNAL_ERROR")
        self.assertIn("3,109", result["text"])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        self.assertEqual(session.store.scenario["quantity_kg"], 3000)

    def test_elapsed_limit_has_own_reason_and_no_request(self):
        session = self.session([])
        with patch("agents.juneok.agent.time.monotonic", side_effect=[0, 91, 92]): result = session.run("비교")
        self.assertEqual(result["status"], "TIME_LIMIT")
        self.assertEqual(result["metrics"]["model_requests"], 0)

    def test_tool_budget_is_distinct_and_keeps_partial_result(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 0}, 1),
                                        call("explain_term", {"topic": "loss_rate"}, 2))], tool_calls=1)
        result = session.run("비교와 설명")
        self.assertEqual(result["status"], "TOOL_LIMIT")
        self.assertEqual(result["termination"]["tool_calls_executed"], 1)
        self.assertIn("3,109", result["text"])

    def test_model_request_budget_separate_from_repeat_stop(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 0}, i)) for i in range(2)], model_requests=2)
        self.assertEqual(session.run("비교")["status"], "LIMIT_REACHED")

    def test_same_invalid_call_stops_early(self):
        session = self.session([response(call("unknown", {}, i)) for i in range(6)])
        result = session.run("잘못된 호출 반복")
        self.assertEqual(result["status"], "REPEATED_TOOL_CALL")
        self.assertEqual(len(result["tool_results"]), 2)
        self.assertEqual(result["metrics"]["model_requests"], 3)

    def test_changed_arguments_can_recover(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": -1})),
                                response(call("compare_shipment_options", {"expected_version": 0, "finish_turn": True}, 2))])
        result = session.run("인수 수정")
        self.assertEqual(result["status"], "OK")
        self.assertIn("3,109", result["text"])

    def test_new_user_turn_can_repeat_same_valid_request(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 0, "finish_turn": True}, i)) for i in range(5)])
        for _ in range(5): self.assertEqual(session.run("같은 조건 다시 비교")["status"], "OK")

    def test_noop_updates_cannot_evade_guard_by_incrementing_version(self):
        session = self.session([response(call("update_scenario", {"expected_version": i, "patch": {"quantity_kg": 3000}}, i)) for i in range(5)])
        result = session.run("동일 값 반복")
        self.assertEqual(result["status"], "REPEATED_TOOL_CALL")
        self.assertEqual(session.store.version, 2)
        self.assertEqual(session.store.scenario["quantity_kg"], 3000)

    def test_guard_allows_state_or_data_progress(self):
        guard = RepeatGuard(allowed=1)
        guard.check("lookup", {}, {"q": 1}, ["dataA"])
        guard.check("lookup", {}, {"q": 2}, ["dataA"])
        guard.check("lookup", {}, {"q": 2}, ["dataB"])
        with self.assertRaises(RunStopped): guard.check("lookup", {}, {"q": 2}, ["dataB"])

    def test_long_dialogue_is_bounded_and_latest_query_survives(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 0, "finish_turn": True}, i)) for i in range(30)], input_characters=8000)
        session.task_context.record("get_historical_shipment_context", self.query(), {"status": "OK"})
        for i in range(30): self.assertEqual(session.run("저장 조건으로 확인 " + str(i))["status"], "OK")
        for request in session.client.requests:
            self.assertLessEqual(encoded_size(request["input"]), 8000)
            self.assertLessEqual(len(user_starts(request["input"])), 4)
        text = session.client.requests[-1]["input"][0]["content"]
        self.assertIn("노지온주", text)
        self.assertEqual(session.store.scenario["current_price_per_kg"], 3000)

    def test_compaction_keeps_active_function_call_output_pair(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 0}, 1)), response()], history_turns=1)
        session.messages = [{"role": "user", "content": "old"}, call("explain_term", {}, 99), {"type": "function_call_output", "call_id": "call_99", "output": "old"}]
        result = session.run("현재 비교")
        self.assertEqual(result["status"], "OK")
        first = session.client.requests[0]["input"]
        self.assertFalse(any(isinstance(x, dict) and x.get("call_id") == "call_99" for x in first))
        second = session.client.requests[1]["input"]
        self.assertTrue(any(getattr(x, "call_id", None) == "call_1" for x in second))
        self.assertTrue(any(isinstance(x, dict) and x.get("call_id") == "call_1" for x in second))

    def test_oversize_active_turn_stops_without_truncating_conditions(self):
        session = self.session([], input_characters=200)
        before = session.store.snapshot(); result = session.run("현재 비교")
        self.assertEqual(result["status"], "CONTEXT_LIMIT")
        self.assertEqual(result["metrics"]["model_requests"], 0)
        self.assertEqual(before, session.store.snapshot())

    def test_explicit_query_changes_and_no_data_are_remembered(self):
        ws = self.workspace(); ws.history(self.query())
        ws.history(self.query(2025))
        self.assertEqual(ws.session.task_context.snapshot()["shipment_query"]["start_year"], 2025)
        self.assertEqual(ws.current_entries()[0]["result"]["status"], "NO_DATA")

    def test_retracted_price_stays_unknown_with_context_and_trimming(self):
        session = self.session([response(call("compare_shipment_options", {"expected_version": 1, "finish_turn": True}, i)) for i in range(6)], history_turns=1)
        session.store.update({}, 0, ["current_price_per_kg"])
        for _ in range(6): session.run("철회한 가격은 미확인으로 유지하고 비교")
        context = session.task_context.snapshot()
        self.assertIsNone(session.store.scenario["current_price_per_kg"])
        self.assertIn("current_price_per_kg", context["pending"]["compare_shipment_options"])

    def test_card_period_change_reconfirms_in_shared_context(self):
        from agents.juneok.workspace import card_values
        ws = self.workspace(); ws.sensitivity("seven_days", [1, 2, 4], [3300])
        self.assertEqual(ws.session.task_context.snapshot()["sensitivity_query"]["delay_days"], 7)
        values, ids = card_values(ws.store.snapshot())
        values.update(days_0=3, period_0=False)
        ws.apply_card(values, ws.store.version, ids)
        self.assertIsNone(ws.store.scenario["options"][0]["loss_percent"])
        self.assertIsNone(ws.session.task_context.snapshot()["sensitivity_query"])
        self.assertTrue(any(x.endswith("loss_percent") for x in ws.session.task_context.snapshot()["pending"]["compare_shipment_options"]))

    def test_case_v2_restores_task_selection_not_archived_facts(self):
        ws = self.workspace(); ws.review(shipment_query=self.query(), loss_values=[1, 2, 4], price_values=[3300])
        raw = ws.export_case("과업 맥락")
        self.assertEqual(json.loads(raw)["version"], 2)
        fresh = self.workspace(); fresh.restore_case(raw, fresh.store.version)
        context = fresh.session.task_context.snapshot()
        self.assertEqual(context["shipment_query"], self.query())
        self.assertEqual(context["goal"], "자료와 조건의 종합 검토")
        self.assertIn("3,109원/kg", render_report(fresh.current_entries()))
        self.assertFalse(any(e["tool"] == "get_historical_shipment_context" for e in fresh.current_entries()))

    def test_case_v1_still_restores(self):
        ws = self.workspace(); document = json.loads(ws.export_case("과거 형식"))
        document["version"] = 1; document["payload"].pop("task_context")
        raw = self.rehash(document)
        self.assertNotIn("task_context", parse_case(raw))
        self.assertEqual(ws.restore_case(raw, ws.store.version)["status"], "OK")

    def test_rehashed_invalid_task_context_is_atomic_rejection(self):
        ws = self.workspace(); document = json.loads(ws.export_case("맥락 검증")); before = ws.store.snapshot()
        document["payload"]["task_context"]["goal"] = "EXECUTE_PRIVATE_COMMAND"
        self.assertEqual(ws.restore_case(self.rehash(document), ws.store.version)["status"], "INVALID_INPUT")
        self.assertEqual(before, ws.store.snapshot())

    def test_clear_chat_keeps_task_but_full_reset_drops_archive(self):
        ws = self.workspace(); ws.history(self.query()); content = ws.export_case("예시")
        ws.restore_case(content, ws.store.version)
        before = ws.session.task_context.snapshot()
        ws.clear_conversation(preserve_conditions=True)
        self.assertEqual(before, ws.session.task_context.snapshot())
        ws.clear_conversation(preserve_conditions=False)
        self.assertIsNone(ws.session.task_context.snapshot()["goal"])
        self.assertFalse(hasattr(ws, "restored_archive"))

    def test_data_change_clears_task_selections_preserving_scenario(self):
        ws = self.workspace(); ws.history(self.query()); before = ws.store.snapshot()
        with patch("agents.juneok.workspace.data_revision", return_value=("NEW",)):
            self.assertTrue(ws.check_data_change())
        self.assertEqual(before, ws.store.snapshot())
        self.assertIsNone(ws.session.task_context.snapshot()["shipment_query"])

    def test_context_cannot_set_numeric_scenario_values(self):
        context = TaskContext(); before = context.snapshot()
        context.record("get_historical_shipment_context", {"shipment_variety": "노지온주", "quantity_kg": 999999}, {"status": "OK"})
        self.assertNotIn("quantity_kg", json.dumps(context.snapshot()))
        self.assertEqual(before["task_id"], context.snapshot()["task_id"])

    def test_context_and_partial_failure_export_pdf_are_consistent(self):
        from pypdf import PdfReader
        from agents.juneok.pdf_report import render_pdf_report
        ws = SharedWorkspace(self.session([response(call("compare_shipment_options", {"expected_version": 0}, i)) for i in range(4)]))
        reply = ws.run("같은 호출 반복")
        self.assertEqual(reply["status"], "REPEATED_TOOL_CALL")
        text = "\n".join(p.extract_text() for p in PdfReader(BytesIO(render_pdf_report(ws.current_entries()))).pages)
        self.assertIn("3,109원/kg", text)
        self.assertIn("요청전체가완료된것은아닙니다", "".join(text.split()))


class CompositeEvaluationTests(unittest.TestCase):
    def test_brief_checks_nested_calculation_and_rejects_wrong_amount(self):
        from agents.juneok.briefing import build_review_brief
        from scripts.evaluate_juneok import score
        result = build_review_brief(demo_scenario())
        turns = [{"status": "OK", "tool_results": [{"tool": "build_review_brief", "result": result}]}]
        case = {"id": "composite", "expected_tool": "compare_shipment_options", "expected_status": "OK", "minimum_price": "3109", "required_tools": ["compare_shipment_options"]}
        self.assertTrue(score(case, turns)["passed"])
        result["components"][0]["result"]["options"][0]["minimum_whole_krw_per_kg"] = "9999"
        self.assertIn("wrong_minimum_price", score(case, turns)["failures"])

    def test_condition_table_checks_actual_hypotheses_and_rejects_wrong_loss(self):
        from agents.juneok.insights import decision_conditions
        from scripts.evaluate_juneok import score
        result = decision_conditions(demo_scenario(), loss_values=[2, 4])
        turns = [{"status": "OK", "tool_results": [{"tool": "explain_decision_conditions", "result": result}]}]
        case = {"id": "condition", "expected_tool": "compare_sensitivity", "expected_status": "OK", "sensitivity_rows": 2, "expected_losses": [2, 4], "expected_prices": [None]}
        self.assertTrue(score(case, turns)["passed"])
        result["cases"][1]["loss_percent"] = "99"
        self.assertIn("wrong_hypothesis_values:loss_percent", score(case, turns)["failures"])


class TaskContextUiTests(unittest.TestCase):
    def test_shared_context_visible_and_full_reset_clears_it(self):
        from streamlit.testing.v1 import AppTest
        root = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            app = AppTest.from_file(str(root / "app.py"), default_timeout=25).run()
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            self.assertTrue(any(e.label == "이번 검토의 선택과 미확인 사항" for e in app.expander))
            next(b for b in app.button if b.label == "과거 출하량 조회").click().run()
            self.assertEqual(app.session_state.workspace.session.task_context.snapshot()["shipment_query"]["shipment_variety"], "노지온주")
            next(b for b in app.button if b.label == "새 비교 시작 · 조건도 초기화").click().run()
            self.assertIsNone(app.session_state.workspace.session.task_context.snapshot()["goal"])
            self.assertFalse(list(app.exception))
