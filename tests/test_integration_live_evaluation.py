"""실제 API 호출 없이 STEP 7 통합 모델 평가기 자체를 검증합니다."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import evaluate_integrated_agent as evaluator


class 통합모델평가기검수(unittest.TestCase):
    def setUp(self):
        self.before = {"version": 1, "scenario": {"options": [{"id": "seven_days", "future_price_per_kg": None}]}}
        self.reply = {"status": "OK", "tool_results": [{"tool": "get_price_history", "result": {
            "status": "OK", "clean_pairs": 20, "interval_days": 7,
            "is_future_forecast": False, "source_verification": {"status": "OK"}}}]}
        self.normal = {"expect": [{"tool": "get_price_history", "status": "OK",
                                   "fields": {"clean_pairs": 20, "interval_days": 7}}],
                       "preserve_scenario": True}

    def test_고정_평가셋의_개수와_다중턴_검증(self):
        cases = evaluator.load_cases()
        self.assertEqual(len(cases), 12)
        self.assertEqual(sum(len(x["turns"]) for x in cases), 16)
        self.assertEqual(len({x["id"] for x in cases}), 12)

    def test_잘못된_평가셋_중복과_빈질문_차단(self):
        cases = evaluator.load_cases()
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "cases.json"
            file.write_text(json.dumps([cases[0], cases[0]], ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "중복"):
                evaluator.load_cases(file)
            wrong = deepcopy(cases[0])
            wrong["turns"][0]["user"] = " "
            file.write_text(json.dumps([wrong], ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "질문"):
                evaluator.load_cases(file)

    def test_문항목록은_실제_API와_가격파일을_읽지_않음(self):
        output = io.StringIO()
        with patch.object(evaluator, "preflight") as check, redirect_stdout(output):
            result = evaluator.main(["--list"])
        self.assertEqual(result, 0)
        self.assertIn("12개 / 자연어 턴: 16개", output.getvalue())
        check.assert_not_called()

    def test_기본_검증은_실제_모델_호출을_하지_않음(self):
        output = io.StringIO()
        with patch.object(evaluator, "preflight", return_value={"status": "OK"}) as check, \
             redirect_stdout(output):
            result = evaluator.main([])
        self.assertEqual(result, 0)
        check.assert_called_once()
        self.assertIn("실제 모델은 호출하지 않았습니다", output.getvalue())

    def test_유료평가_실행범위_명시가_없으면_거부(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                evaluator.main(["--live"])
        self.assertEqual(stopped.exception.code, 2)

    def test_알수없는_평가_문항은_API_호출전에_거부(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                evaluator.main(["--live", "--case", "INT-99"])
        self.assertEqual(stopped.exception.code, 2)

    def test_정상_가격쌍과_원천검증_성공을_구별해_검사(self):
        result = evaluator.score_turn(self.normal, self.reply, self.before, deepcopy(self.before),
                                      {"interval_days": 7}, [{"tool": "get_price_history", "args": {}}])
        self.assertTrue(result["passed"], result["failures"])

    def test_앱에서_보충_실행했어도_모델이_직접_도구를_선택해야_통과(self):
        result = evaluator.score_turn(self.normal, self.reply, self.before, deepcopy(self.before),
                                      {"interval_days": 7}, [])
        self.assertIn("expected_tool_not_selected_by_model:get_price_history", result["failures"])

    def test_필요한_도구를_실행하지_않으면_실패(self):
        result = evaluator.score_turn(self.normal, {"status": "OK", "tool_results": []},
                                      self.before, deepcopy(self.before), {}, [])
        self.assertIn("missing_tool:get_price_history", result["failures"])

    def test_미검증_가격을_성공으로_위장하지_못함(self):
        reply = deepcopy(self.reply)
        reply["tool_results"][0]["result"]["source_verification"]["status"] = "DATA_ERROR"
        result = evaluator.score_turn(self.normal, reply, self.before, deepcopy(self.before), {},
                                      [{"tool": "get_price_history", "args": {}}])
        self.assertIn("history_source_not_verified", result["failures"])

    def test_후속_모델_인수의_참거짓을_검사(self):
        spec = {"model_args": {"get_price_history": {"follow_up": True}}}
        call = [{"tool": "get_price_history", "args": {"follow_up": False}}]
        result = evaluator.score_turn(spec, self.reply, self.before, deepcopy(self.before), None, call)
        self.assertIn("wrong_model_arg:get_price_history:follow_up", result["failures"])

    def test_자료_자동적용_시장의_후속조회_유입차단(self):
        spec = {"history_absent": ["market_code", "trade_type"]}
        result = evaluator.score_turn(spec, self.reply, self.before, deepcopy(self.before),
                                      {"market_code": "110001", "trade_type": "경매"}, [])
        self.assertEqual(len(result["failures"]), 2)
        self.assertIn("inherited_unconfirmed_history_field:market_code", result["failures"])

    def test_시나리오_변경과_미래가격_자동입력을_각각_실패로_판정(self):
        after = deepcopy(self.before)
        after["scenario"]["options"][0]["future_price_per_kg"] = "3200"
        spec = {"preserve_scenario": True, "future_price_must_be_empty": True,
                "forbid_tools": ["update_scenario"]}
        result = evaluator.score_turn(spec, {"status": "OK", "tool_results": [{
            "tool": "update_scenario", "result": {"status": "OK"}}]}, self.before, after, None, [])
        self.assertIn("unexpected_scenario_mutation", result["failures"])
        self.assertIn("future_price_autofilled", result["failures"])
        self.assertIn("forbidden_tool:update_scenario", result["failures"])

    def test_불확실한_중량은_조회_성공_금지를_판정(self):
        spec = {"forbid_success": ["get_price_history"]}
        result = evaluator.score_turn(spec, self.reply, self.before, deepcopy(self.before), None, [])
        self.assertIn("forbidden_success:get_price_history", result["failures"])

    def test_실제_호출인수는_모델_함수호출_객체에서_읽음(self):
        session = SimpleNamespace(messages=[
            {"role": "user", "content": "원래 질문"},
            SimpleNamespace(type="function_call", name="get_price_history", call_id="abc",
                            arguments='{"follow_up": true}'),
            {"type": "function_call_output", "output": "{}"}])
        result = evaluator.model_calls(session)
        self.assertEqual(result, [{"call_id": "abc", "tool": "get_price_history", "args": {"follow_up": True}}])

    def test_집계에서_중단과_사용량_누적을_분리(self):
        cases = evaluator.load_cases()[:2]
        runs = [{"passed": True, "category": "가격조회", "turns": [{"failures": [],
                 "metrics": {"model_requests": 1, "input_tokens": 100, "output_tokens": 15}}]},
                {"passed": False, "category": "다중턴", "turns": [{"failures": ["missing_tool:x"],
                 "metrics": {"model_requests": 2, "input_tokens": 150, "output_tokens": 20}}]}]
        summary = evaluator.summarize(cases, runs, 1, "test-model")
        self.assertEqual((summary["completed_runs"], summary["passed_runs"]), (2, 1))
        self.assertEqual(summary["usage"]["input_tokens"], 250)
        self.assertEqual(summary["failure_counts"]["missing_tool:x"], 1)
        self.assertIsNone(summary["estimated_cost_usd"])

    def test_숫자_표시차이는_같고_알수없는_필드는_다름(self):
        self.assertTrue(evaluator.equal_values("5.00", 5))
        self.assertFalse(evaluator.equal_values(False, 0))
        self.assertEqual(evaluator.read_path({"options": [{"price": "3109"}]}, "options.0.price"), "3109")
        with self.assertRaises(KeyError):
            evaluator.read_path({"options": []}, "options.0.price")

    def test_데이터_버전_파일누락은_거부(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                evaluator.source_hashes(Path(folder))

    def test_도구가_맞더라도_보고서_품질검증_실패면_불합격(self):
        class DummyStore:
            def snapshot(self):
                return {"version": 0, "scenario": {"options": []}}
        class DummyWS:
            def __init__(self):
                self.store = DummyStore()
                self.session = SimpleNamespace(messages=[], price_history_query=None)
            def run(self, question):
                return {"status": "OK", "tool_results": [], "text": "잘못된 보고서", "metrics": {}}
        ws = DummyWS()
        case = {"id": "INT-99", "category": "검증", "title": "검증", "setup": "empty",
                "turns": [{"user": "조회", "expect": []}]}
        with patch.object(evaluator, "create_workspace", return_value=ws), \
             patch.object(evaluator, "turn_quality", return_value={"passed": False, "checks": {"report_matches_visible_results": False}}):
            result = evaluator.run_case(case, SimpleNamespace())
        self.assertFalse(result["passed"])
        self.assertIn("outcome_quality_check_failed", result["turns"][0]["failures"])

    def test_도구의_인증오류가_발생하면_같은_문항의_다음턴을_호출하지_않음(self):
        class DummyStore:
            def snapshot(self):
                return {"version": 0, "scenario": {"options": []}}
        class DummyWS:
            def __init__(self):
                self.store = DummyStore()
                self.session = SimpleNamespace(messages=[], price_history_query=None)
                self.requests = 0
            def run(self, question):
                self.requests += 1
                return {"status": "AUTH_ERROR", "tool_results": [], "text": "인증 실패"}
        ws = DummyWS()
        case = {"id": "INT-99", "category": "오류", "title": "오류", "setup": "empty",
                "turns": [{"user": "첫 요청", "expect": []}, {"user": "둘째 요청", "expect": []}]}
        with patch.object(evaluator, "create_workspace", return_value=ws), \
             patch.object(evaluator, "turn_quality", return_value={"passed": True, "checks": {}}):
            result = evaluator.run_case(case, SimpleNamespace())
        self.assertEqual(result["abort_reason"], "AUTH_ERROR")
        self.assertEqual(result["completed_turns"], 1)
        self.assertEqual(ws.requests, 1)


if __name__ == "__main__":
    unittest.main()
