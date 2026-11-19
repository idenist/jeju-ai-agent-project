"""실제 API 호출 없이 STEP 7 통합 모델 평가기 자체를 검증합니다."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date
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
        self.assertEqual(len(cases), 36)
        self.assertEqual(sum(len(x["turns"]) for x in cases), 148)
        self.assertEqual(len({x["id"] for x in cases}), 36)
        self.assertEqual([x["id"] for x in cases if x["setup"] == "candidate_comparison"],
                         [f"INT-{n}" for n in range(23, 29)])
        fresh = [x for x in cases if x["turns"][0].get("candidate", {}).get("opens")]
        self.assertEqual([x["id"] for x in fresh], [f"INT-{n}" for n in range(29, 37)])
        self.assertTrue(all(x["setup"] == "empty" and sum(t.get("require_model", False) for t in x["turns"]) == 1
                            for x in fresh))

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
        self.assertIn("36개 / 자연어 턴: 138개 / 폼: 10단계", output.getvalue())
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

    def test_지정_자료_버전과_활성_자료가_다르면_실행하지_않는다(self):
        with patch.object(evaluator, "preflight", return_value={"status": "OK", "snapshot_id": "wrong"}), \
             patch.object(evaluator, "run_case") as run, redirect_stderr(io.StringIO()):
            result = evaluator.main(["--live", "--case", "INT-23", "--snapshot-id", "expected"])
        self.assertEqual(result, 2)
        run.assert_not_called()

    def test_평가_날짜를_한_문항의_모든_턴에_적용한다(self):
        def observed_dates(*_):
            from services.data_refresh import kst_today
            return [kst_today(), kst_today()]
        with patch.object(evaluator, "_run_case", side_effect=observed_dates):
            chosen = date(2026, 11, 14)
            self.assertEqual(evaluator.run_case({}, None, as_of=chosen), [chosen, chosen])

    def test_정상_가격쌍과_원천검증_성공을_구별해_검사(self):
        result = evaluator.score_turn(self.normal, self.reply, self.before, deepcopy(self.before),
                                      {"interval_days": 7}, [{"tool": "get_price_history", "args": {}}])
        self.assertTrue(result["passed"], result["failures"])

    def test_재사용_제안과_실제_중복_조회는_다르게_채점한다(self):
        spec = deepcopy(self.normal)
        spec["expect"][0]["count"] = 1
        spec["model_args"] = {"get_price_history": {"follow_up": False}}
        calls = [{"tool": "get_price_history", "args": {"follow_up": False}, "call_id": "original"},
                 {"tool": "get_price_history", "args": {"follow_up": True}, "call_id": "reused"}]
        reply = {**self.reply, "visible_results": self.reply["tool_results"],
                 "metrics": {"reused_price_history_calls": ["reused"]}}
        self.assertTrue(evaluator.score_turn(spec, reply, self.before, self.before, None, calls)["passed"])
        duplicated = {**reply, "tool_results": self.reply["tool_results"] * 2, "metrics": {}}
        verdict = evaluator.score_turn(spec, duplicated, self.before, self.before, None, calls)
        self.assertIn("wrong_tool_count:get_price_history", verdict["failures"])
        self.assertIn("wrong_model_arg:get_price_history:follow_up", verdict["failures"])

    def test_앱에서_보충_실행했어도_모델이_직접_도구를_선택해야_통과(self):
        result = evaluator.score_turn(self.normal, self.reply, self.before, deepcopy(self.before),
                                      {"interval_days": 7}, [])
        self.assertIn("expected_tool_not_selected_by_model:get_price_history", result["failures"])

    def test_상자_무게_확인처럼_로컬_처리를_기대한_턴을_구분한다(self):
        spec = deepcopy(self.normal)
        spec["expect"][0]["model_selected"] = False
        no_call = evaluator.score_turn(spec, self.reply, self.before, self.before, None, [])
        self.assertTrue(no_call["passed"], no_call["failures"])
        unexpected_call = evaluator.score_turn(spec, self.reply, self.before, self.before,
                                               None, [{"tool": "get_price_history", "args": {}}])
        self.assertIn("unexpected_model_tool_selection:get_price_history", unexpected_call["failures"])

    def test_필요한_도구를_실행하지_않으면_실패(self):
        result = evaluator.score_turn(self.normal, {"status": "OK", "tool_results": []},
                                      self.before, deepcopy(self.before), {}, [])
        self.assertIn("missing_tool:get_price_history", result["failures"])

    def test_미래_출하에서_A안_금액이_같아도_조건이_바뀌면_실패(self):
        expected = {"tool": "compare_future_pair", "status": "OK", "same_fields_as_turn": {"option_a": 1}}
        prior = {"tool": "compare_future_pair", "result": {"status": "OK", "option_a": {
            "proceeds_krw": "7018750", "sale_date": "2026-11-18"}}}
        reply = {"status": "OK", "tool_results": [deepcopy(prior)]}
        call = {"tool": "compare_future_pair", "args": {}}
        def score():
            return evaluator.score_turn({"expect": [expected]}, reply, self.before, self.before,
                                        None, [call], previous_turns=[{"tool_results": [prior]}])
        self.assertTrue(score()["passed"])
        reply["tool_results"][0]["result"]["option_a"]["sale_date"] = "2026-11-19"
        self.assertIn("changed_field:compare_future_pair:option_a:turn_1", score()["failures"])

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

    def test_예측_거절은_도구_이름보다_응답과_저장_결과로_채점한다(self):
        spec = {"forecast_refusal": True, "preserve_scenario": True, "future_price_must_be_empty": True}
        answer = {"status": "OK", "text": "과거 가격만으로 다음 주 실제 단가를 확정할 수는 없습니다."}
        self.assertTrue(evaluator.score_turn(spec, answer, self.before, self.before, None, [])["passed"])
        answer["text"] += " 그래도 다음 주 단가는 3,000원입니다."
        self.assertIn("unsupported_forecast_price_claim",
                      evaluator.score_turn(spec, answer, self.before, self.before, None, [])["failures"])

    def test_후보일_상태와_근거_문서를_독립해_검사한다(self):
        before = {"dates": ["2026-11-23", "2026-11-28"], "conditions": {"basis": "gross"}, "losses": {},
                  "prices": {"2026-11-23": 2500, "2026-11-28": None}, "pending_date": None,
                  "report_id": "CIT-OLD", "revision": 0}
        after = {**before, "prices": {"2026-11-23": 2500, "2026-11-28": "2600.00"},
                 "report_id": "CIT-NEW", "revision": 1, "result": {"price_basis": "gross"}}
        spec = {"report": "changed", "pending_date": None, "prices": {"2026-11-23": 2500, "2026-11-28": 2600},
                "report_fields": {"price_basis": "gross"}}
        self.assertEqual(evaluator._candidate_failures(spec, before, after, True), [])
        after.update(report_id=before["report_id"], prices={"2026-11-23": 2600, "2026-11-28": None})
        self.assertEqual(set(evaluator._candidate_failures(spec, before, after, False)),
                         {"unexpected_future_pair_mutation", "wrong_candidate_prices", "candidate_report_not_refreshed"})

    def test_빈_상태_후보일_평가의_실제_모델_단계는_키가_없으면_통과하지_않는다(self):
        from core.config import Settings

        for case in (case for case in evaluator.load_cases() if case["turns"][0].get("candidate", {}).get("opens")):
            with self.subTest(case=case["id"]):
                result = evaluator.run_case(case, Settings())
                self.assertEqual(result["completed_turns"], result["expected_turns"])
                failed = [turn for turn in result["turns"] if not turn["passed"]]
                self.assertEqual(len(failed), 1, [(turn["turn_index"], turn["failures"]) for turn in failed])
                self.assertIn("missing_real_model_request", failed[0]["failures"])

    def test_출하량_원천_파일이_없어도_후보일을_유지하고_견적을_등록한다(self):
        from core.config import Settings

        case = deepcopy(next(case for case in evaluator.load_cases() if case["id"] == "INT-32"))
        case["turns"] = case["turns"][:5]
        lookup = case["turns"][3]["expect"][0]
        lookup.update(status="DATA_ERROR", fields={"is_future_forecast": False, "error_kind": "FileNotFoundError"})
        with patch("services.shipment_timing._load", side_effect=FileNotFoundError):
            result = evaluator.run_case(case, Settings())
        self.assertTrue(result["passed"], [turn["failures"] for turn in result["turns"]])

    def test_같은_후보일_출하량_재조회에_실패하면_이전_다운로드를_숨긴다(self):
        from core.config import Settings
        from ui.chat import _chat_report, _requested_chat_report

        case = next(case for case in evaluator.load_cases() if case["id"] == "INT-32")
        turns = case["turns"]
        workspace = evaluator.create_workspace(Settings(), "empty")
        with patch("services.data_refresh.kst_today", return_value=date(2026, 11, 14)):
            workspace.run(turns[0]["user"])
            self.assertTrue(evaluator._submit_candidate_form(workspace, turns[1]["candidate_form"]))
            workspace.run(turns[2]["user"])
            first = workspace.run(turns[3]["user"])
            first["chat_report"] = _chat_report(workspace, first)
            messages = [{"reply": first}]
            self.assertEqual(_requested_chat_report(workspace, messages, "timing")["kind"], "shipment_timing")
            with patch("services.shipment_timing._load", side_effect=FileNotFoundError):
                failed_lookup = workspace.run(turns[2]["user"])
            self.assertEqual(failed_lookup["visible_results"][0]["result"]["status"], "DATA_ERROR")
            failed_lookup["chat_report"] = _chat_report(workspace, failed_lookup)
            messages.append({"reply": failed_lookup})
        self.assertIsNone(_requested_chat_report(workspace, messages, "timing"))
        self.assertIsNone(_requested_chat_report(workspace, messages, "comparison"))

    def test_보관비_정정_확인_중에도_같은_날짜의_출하량_보고서는_받을_수_있다(self):
        from core.config import Settings
        from ui.chat import _chat_report, _requested_chat_report

        case = next(case for case in evaluator.load_cases() if case["id"] == "INT-36")
        turns = case["turns"]
        workspace = evaluator.create_workspace(Settings(), "empty")
        with patch("services.data_refresh.kst_today", return_value=date(2026, 11, 14)):
            workspace.run(turns[0]["user"])
            self.assertTrue(evaluator._submit_candidate_form(workspace, turns[1]["candidate_form"]))
            for turn in turns[2:8]:
                workspace.run(turn["user"])
            timing = workspace.run("같은 두 날짜의 과거 출하량도 보여 주세요.")
            timing["chat_report"] = _chat_report(workspace, timing)
            messages = [{"reply": timing}]
            timing_report = _requested_chat_report(workspace, messages, "timing")
            self.assertEqual(timing_report["kind"], "shipment_timing")
            self.assertEqual(_requested_chat_report(workspace, messages, "comparison")["kind"], "shipment_candidates")

            example = deepcopy(timing)
            example["metrics"]["dialogue_route"] = "read_only_conditions"
            example["chat_report"]["entries"][0]["result"]["input"]["shipment_variety"] = "레드향"
            messages.append({"reply": example})
            self.assertEqual(_requested_chat_report(workspace, messages, "timing")["json"], timing_report["json"])

            pending = workspace.run("보관비 청구 방식을 바꿔 주세요")
            messages.append({"reply": pending})
            self.assertIsNone(_requested_chat_report(workspace, messages, "comparison"))
            self.assertEqual(_requested_chat_report(workspace, messages, "timing")["json"], timing_report["json"])

            updated = workspace.run("정액 말고 일할로 해주세요")
            messages.append({"reply": updated})
            self.assertEqual(workspace.session.current_shipment_candidates().result["storage_billing"]["mode"], "prorated")
            self.assertEqual(_requested_chat_report(workspace, messages, "comparison")["kind"], "shipment_candidates")
            self.assertEqual(_requested_chat_report(workspace, messages, "timing")["json"], timing_report["json"])

            draft = workspace.session.current_shipment_candidates()
            revision, result = draft.revision, deepcopy(draft.result)
            workspace.run(turns[-1]["user"])
            self.assertEqual(draft.revision, revision)
            self.assertEqual(draft.result, result)

            for text in ("정액 말고 일할로 해주세요", "정액이 아니라 일할이래요"):
                with self.subTest(correction=text):
                    workspace.run("보관비는 정액으로 바꿔 주세요")
                    self.assertEqual(draft.result["storage_billing"]["mode"], "fixed")
                    workspace.run(text)
                    self.assertEqual(draft.result, result)

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
        runs = [{"id": cases[0]["id"], "passed": True, "category": "가격조회",
                 "turns": [{"failures": [], "passed": True, "response_seconds": 2,
                 "metrics": {"model_requests": 1, "input_tokens": 100, "output_tokens": 15}}]},
                {"id": cases[1]["id"], "passed": False, "category": "다중턴",
                 "abort_reason": "API_TIMEOUT",
                 "turns": [{"failures": ["missing_tool:x"], "passed": False, "response_seconds": 3,
                 "metrics": {"model_requests": 2, "input_tokens": 150, "output_tokens": 20}}]}]
        summary = evaluator.summarize(cases, runs, 1, "test-model")
        self.assertEqual((summary["completed_runs"], summary["passed_runs"]), (2, 1))
        self.assertEqual(summary["usage"]["input_tokens"], 250)
        self.assertEqual(summary["failure_counts"]["missing_tool:x"], 1)
        self.assertEqual(summary["case_metrics"][cases[1]["id"]]["aborts"], {"API_TIMEOUT": 1})
        self.assertIsNone(summary["estimated_cost_usd"])

    def test_문항별_첫_성공_응답의_중앙값과_최댓값을_집계(self):
        def sample(case_id, seconds, passed=True):
            return {"id": case_id, "passed": passed, "category": "다중턴", "turns": [
                {"passed": passed, "response_seconds": seconds, "failures": [],
                 "run_status": "OK" if passed else "API_TIMEOUT"},
                {"passed": True, "response_seconds": 99, "failures": []}]}
        reports = [sample("INT-13", 15), sample("INT-13", 5), sample("INT-13", 9),
                   sample("INT-13", 91, passed=False), sample("INT-01", 40)]
        summary = evaluator.summarize([{"id": "INT-13"}, {"id": "INT-01"}], reports, 3, "test-model")
        self.assertEqual(summary["first_response_seconds"]["INT-13"], {"samples": [15, 5, 9], "median": 9, "max": 15})
        row = summary["case_metrics"]["INT-13"]
        self.assertEqual((row["passed"], row["completed"]), (3, 4))
        self.assertEqual(row["first_response_all"], {"samples": [15, 5, 9, 91], "median": 12, "max": 91})
        self.assertEqual(row["failed_turn_statuses"], {"API_TIMEOUT": 1})
        self.assertEqual(row["failed_turn_response"]["max"], 91)

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
