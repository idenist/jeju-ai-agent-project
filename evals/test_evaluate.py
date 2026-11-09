"""평가기 자체의 정확성을 검증한다. 실제 LLM API는 호출하지 않는다."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.agent import execute_tool, format_verified_result, run_turn
import evals.evaluate as evaluator
from evals.compare import compare_runs
from evals.evaluate import (
    DEFAULT_CSV, DEFAULT_DATASET, DEFAULT_MANIFEST,
    RecordingClient, grade_step, load_cases, match_arg, summarize, validate_versions,
)


BASE_ARGS = {
    "variety":"조생귤","grade":"특","size":"소","package_kg":5,
    "quantity_kg":3000,"delay_days":7,"daily_storage_cost":20000,
    "loss_percent":2,"current_price_per_kg":None,"future_price_per_kg":None,
}


def response_event(name, args, result):
    return [
        {"functions":[{"name":name,"args":args,"call_id":"call1"}],"tool_outputs":[]},
        {"functions":[],"tool_outputs":[{"call_id":"call1","result":result}]},
    ]


class FakeSdkResponses:
    """Responses API의 함수 호출과 결과 수신을 흉내 내며 래퍼의 추적까지 검사한다."""
    def __init__(self, args):
        self.args = args
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls)==1:
            return SimpleNamespace(
                id="resp_1",status="completed",output_text="",
                output=[SimpleNamespace(type="function_call",name="compare_shipping",arguments=json.dumps(self.args),call_id="call_1")],
                usage=SimpleNamespace(input_tokens=150,output_tokens=60,total_tokens=210),
            )
        return SimpleNamespace(
            id="resp_2",status="completed",output_text="매출 7,340,430원입니다.",output=[],
            usage=SimpleNamespace(input_tokens=120,output_tokens=30,total_tokens=150),
        )


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases(DEFAULT_DATASET)
        cls.gold = cls.cases[0]["turns"][0]["expected"]
        cls.result = execute_tool("compare_shipping", BASE_ARGS, DEFAULT_CSV)
        cls.answer = format_verified_result("compare_shipping", cls.result)
        cls.events = response_event("compare_shipping", BASE_ARGS, cls.result)

    def test_dataset_schema_and_counts(self):
        self.assertEqual(len(self.cases),30)
        self.assertEqual(sum(len(c["turns"]) for c in self.cases),33)
        self.assertEqual(sum(c["split"]=="dev" for c in self.cases),20)
        self.assertEqual(sum(c["split"]=="holdout" for c in self.cases),10)
        self.assertEqual(len(set(c["id"] for c in self.cases)),30)

    def test_v0_files_match_fixed_manifest(self):
        manifest=json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        checks=validate_versions(manifest,DEFAULT_CSV,allow_mismatch=False)
        self.assertEqual(len(checks),3)
        self.assertTrue(all(item["matched"] for item in checks))

    def test_correct_comparison_passes(self):
        graded=grade_step(self.gold,self.answer,self.events)
        self.assertTrue(graded["passed"],graded["notes"])
        self.assertTrue(graded["tool_result_accuracy_ok"])

    def test_recomputed_rounded_revenue_fails(self):
        wrong_answer=self.answer.replace("7,340,424원","7,340,430원")
        graded=grade_step(self.gold,wrong_answer,self.events)
        self.assertFalse(graded["passed"])
        self.assertFalse(graded["numeric_accuracy_ok"])

    def test_wrong_tool_arguments_fail_even_when_report_is_correct(self):
        bad=dict(BASE_ARGS,loss_percent=5)
        graded=grade_step(self.gold,self.answer,response_event("compare_shipping",bad,self.result))
        self.assertFalse(graded["passed"])
        self.assertFalse(graded["parameter_accuracy_ok"])

    def test_wrong_tool_result_fails_even_when_report_is_correct(self):
        wrong=dict(self.result,current_gross_krw="7,340,430")
        graded=grade_step(self.gold,self.answer,response_event("compare_shipping",BASE_ARGS,wrong))
        self.assertFalse(graded["passed"])
        self.assertFalse(graded["tool_result_accuracy_ok"])

    def test_missing_information_question_and_premature_lookup(self):
        expected={"action":"ask","missing":["grade","size","daily_storage_cost"]}
        question="등급, 크기, 하루 보관비가 얼마인지 알려 주세요."
        graded=grade_step(expected,question,[])
        self.assertTrue(graded["passed"])
        self.assertTrue(all(graded["missing_coverage"].values()))
        with_lookup=grade_step(expected,question,[{"functions":[{"name":"get_market_price"}],"tool_outputs":[]}])
        self.assertTrue(with_lookup["passed"])
        self.assertTrue(with_lookup["unnecessary_lookup"])
        with_compare=grade_step(expected,question,[{"functions":[{"name":"compare_shipping"}],"tool_outputs":[]}])
        self.assertFalse(with_compare["passed"])

    def test_invalid_error_rejects_calculation(self):
        exp={"action":"reject","reason":"loss_percent"}
        self.assertTrue(grade_step(exp,"감량률 100%는 계산할 수 없습니다.",[])["passed"])
        self.assertFalse(grade_step(exp,self.answer,self.events)["passed"])

    def test_price_missing_and_price_no_data(self):
        case=self.cases[11]
        self.assertEqual(case["id"],"EVAL-012")
        exp=case["turns"][0]["expected"]
        events=response_event("get_market_price",exp["args"],{"has_price":False,"reason":"조회 거래 없음"})
        self.assertTrue(grade_step(exp,"해당 규격의 거래가 없어 조회할 수 없습니다.",events)["passed"])
        self.assertFalse(grade_step(exp,"9,999원/kg입니다.",events)["passed"])

    def test_mocked_responses_roundtrip_and_scoring(self):
        sdk=SimpleNamespace(responses=FakeSdkResponses(BASE_ARGS))
        client=RecordingClient(sdk)
        text,_=run_turn(client,"조생귤 3톤 7일 보관하면?",prices_file=DEFAULT_CSV)
        self.assertEqual(len(client.responses.events),2)
        self.assertTrue(grade_step(self.gold,text,client.responses.events)["passed"])
        self.assertIn("7,340,424원",text)
        self.assertNotIn("7,340,430원",text)
        self.assertEqual(sum(e["tokens"]["total"] for e in client.responses.events),360)

    def test_summary_counts_api_errors_as_failures(self):
        failed={"id":"bad","category":"compare","steps":[{"error_type":"AuthenticationError"}]}
        s=summarize([failed],model="test",seconds=0,prices={"input":None,"output":None})
        self.assertEqual(s["cases_passed"],0)
        self.assertEqual(s["steps_attempted"],1)
        self.assertEqual(s["step_pass_rate"],0)
        self.assertEqual(s["api_or_agent_errors"],1)

    def test_full_cli_writes_real_result_schema_with_fake_sdk(self):
        """API를 호출하지 않고도 CLI→Agent→채점→파일 저장 전체 흐름을 검증한다."""
        fake=SimpleNamespace(responses=FakeSdkResponses(BASE_ARGS))
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ,{"OPENAI_API_KEY":"test-key"}):
                with patch.dict(sys.modules,{"openai":SimpleNamespace(OpenAI=lambda:fake)}):
                    status=evaluator.main([
                        "--live","--split","all","--case-id","EVAL-001",
                        "--out-dir",temp,
                    ])
            self.assertEqual(status,0)
            report=json.loads((Path(temp)/"summary.json").read_text(encoding="utf-8"))
            self.assertEqual(report["cases_passed"],1)
            self.assertEqual(report["steps_passed"],1)
            self.assertEqual(report["api_calls"],2)
            self.assertEqual(report["token_usage"]["total"] if "total" in report["token_usage"] else report["token_usage"]["input"],270)
            self.assertTrue((Path(temp)/"metadata.json").exists())
            self.assertTrue((Path(temp)/"cases.jsonl").exists())

    def test_compare_rejects_different_dataset_or_model(self):
        base_meta={"dataset_sha256":"abc","model":"same","case_ids":["EVAL-001"],"version_checks":[{"name":"daily_prices","actual":"stable_data_sha"}]}
        base_summary={"case_pass_rate":1.0,"step_pass_rate":1.0,"api_calls":2,"latency_seconds":{"mean":1.0},"token_usage":{"input":100,"output":10}}
        base_cases=[{"id":"EVAL-001","trial":1,"steps":[{"evaluation":{"passed":True}}]}]
        self.assertEqual(len(compare_runs((base_meta,base_summary,base_cases),(base_meta,base_summary,base_cases))["cases_regressed"]),0)
        with self.assertRaisesRegex(ValueError,"해시"):
            compare_runs((base_meta,base_summary,base_cases),({**base_meta,"dataset_sha256":"xyz"},base_summary,base_cases))
        with self.assertRaisesRegex(ValueError,"모델"):
            compare_runs((base_meta,base_summary,base_cases),({**base_meta,"model":"other"},base_summary,base_cases))

    def test_numeric_argument_equality(self):
        self.assertTrue(match_arg(2,2.0))
        self.assertTrue(match_arg(None,None))
        self.assertFalse(match_arg(None,0))
        self.assertFalse(match_arg(True,1))


if __name__=="__main__":
    unittest.main()
