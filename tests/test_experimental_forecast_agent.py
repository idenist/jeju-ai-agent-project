"""운영 비활성 연구용 예측 도구 계약. 외부 API/파일을 변경하지 않는다."""
import csv
import json
from types import SimpleNamespace
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.seunghyuk.forecast_agent import (
    EXPERIMENTAL_TOOL, ExperimentalForecastDraft, _resolve_spec,
    format_experimental_result, model_receipt,
)


SPEC = {"market_code": None, "variety": "조생귤", "grade": "특", "size": "소",
        "package": "상자", "package_kg": 5, "trade_type": None}
OPTION_A = {"delay_days": 3, "loss_percent": 1, "daily_storage_cost": 10000,
            "other_cost_krw": 5000, "fee_percent": 5}
OPTION_B = {"delay_days": 14, "loss_percent": 4, "daily_storage_cost": 10000,
            "other_cost_krw": 5000, "fee_percent": 7}
COMPLETE = {"as_of": "2026-11-03", "quantity": 3000, "price_basis": "gross",
            "spec": SPEC, "option_a": OPTION_A, "option_b": OPTION_B}
EXAMPLE_RESULT = {"status": "EXPERIMENTAL_ONLY", "deployment_approved": False,
                  "provenance": {"as_of": "2026-11-03", "age_days": 11, "forecasts": {
                      "3": {"target_date": "2026-11-06", "price_krw_per_kg": "2558.40"},
                      "14": {"target_date": "2026-11-17", "price_krw_per_kg": "3107.78"}},
                      "warnings": ["관측자료가 오래되었습니다."]},
                  "calculation": {"option_a": {"proceeds_krw": "7183525.6"},
                      "options_b": [{"proceeds_krw": "8178877.952", "difference_vs_a_krw": "995352.352"}],
                      "minimum_whole_krw_per_kg": "2737"},
                  "message": "연구용 리플레이"}
RESOLVED = {"spec": {"market_code": "110001", "variety_code": "01", "grade_code": "11", "size_code": "184",
                    "package": "상자", "package_kg": "5.000", "trade_type": "경매"},
            "applied_data_defaults": {"market_code": "110001", "trade_type": "경매"}}


class ExperimentalAgentBridgeTests(unittest.TestCase):
    def test_기본_도구와_분리된_엄격한_스키마(self):
        self.assertEqual(EXPERIMENTAL_TOOL["name"], "compare_experimental_forecast")
        schema = EXPERIMENTAL_TOOL["parameters"]
        self.assertTrue(EXPERIMENTAL_TOOL["strict"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])

    def test_일상적인_짧은_출하질문을_ML실험으로_보냄(self):
        # 기능 설명을 아는 사용자는 극소수다. CLI 옵트인 환경에서는
        # '연구용 ML 모델' 문구가 없어도 출하 시점 비교 의도를 인식해야 한다.
        from agents.seunghyuk.forecast_agent import keep_experimental_route
        draft = ExperimentalForecastDraft()
        cases = (
            "감귤 3톤 있는데 언제 팔까요?",
            "조생귤 3톤 3일 뒤와 14일 뒤 중 언제 출하하는 게 나아요?",
            "귤 이번 주에 팔까요 다음 주에 팔까요?",
            "3일 뒤랑 10일 뒤 중 어느 때 출하하는 게 좋아요?",
            "조생귤 보관하는 게 유리할까요?",
        )
        for question in cases:
            with self.subTest(question=question):
                self.assertTrue(keep_experimental_route(question, draft))

    def test_과거조회나_출하량질문은_실험으로_탈취하지_않음(self):
        from agents.seunghyuk.forecast_agent import keep_experimental_route
        draft = ExperimentalForecastDraft()
        for question in (
                "감귤 과거 7일 가격 비교해줘",
                "이번 주 감귤 출하량 알려줘",
                "조생귤 오늘 경락가격이 얼마예요?",
                "감모율이 무엇인가요?",
                "일반 계산으로 해줘",
                "감귤 다음주 가격을 확정해줘"):
            with self.subTest(question=question):
                self.assertFalse(keep_experimental_route(question, draft))

    def test_출하일과_세부품종_미확인은_값을_만들지_않고_질문(self):
        draft = ExperimentalForecastDraft()
        result = draft.apply({"action": "new", "patch": {
            "quantity": 3000, "spec": {"variety": "감귤"}}})
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("option_a.delay_days", result["missing_fields"])
        self.assertIn("option_b.delay_days", result["missing_fields"])
        self.assertIn("spec.variety", result["missing_fields"])
        self.assertEqual(result["missing_fields"][:3], [
            "option_a.delay_days", "option_b.delay_days", "spec.variety"])
        self.assertIsNone(draft.inputs["spec"]["variety"])
        # 직전 비용 질문을 잠시 중단하고 용어 설명을 요청할 수 있어야 한다.
        from agents.seunghyuk.forecast_agent import keep_experimental_route
        self.assertFalse(keep_experimental_route("감모율이 뭐예요?", draft))
        self.assertTrue(draft.awaiting_input)  # 이전 ML 과업은 삭제하지 않음
        message = format_experimental_result(result)
        self.assertIn("A안 보관일", message)
        self.assertIn("B안 보관일", message)
        self.assertNotIn("다음에 확인할 조건:", message)

    def test_모르는_가격_비용을_임의로_채우지_않음(self):
        state = ExperimentalForecastDraft()
        result = state.apply({"action": "new", "patch": {"quantity": 3000}})
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("as_of", result["missing_fields"])
        self.assertIn("option_a.loss_percent", result["missing_fields"])
        self.assertNotIn("2558.40", format_experimental_result(result))

    def test_비지원_10일은_모델을_열지_않고_거부(self):
        state = ExperimentalForecastDraft()
        with patch("agents.seunghyuk.forecast_agent.compare_experimental", side_effect=AssertionError):
            result = state.apply({"action": "new", "patch": {
                "option_a": {"delay_days": 3}, "option_b": {"delay_days": 10}}})
        self.assertEqual(result["status"], "NO_DATA")
        self.assertIn("3·7·14일", result["message"])

    def test_보관일_수정하면_새_감모_다시_확인(self):
        state = ExperimentalForecastDraft()
        state.apply({"action": "new", "patch": {"option_b": {"delay_days": 14, "loss_percent": 4}}})
        state.apply({"action": "edit", "patch": {"option_b": {"delay_days": 7, "loss_percent": 4}}})
        self.assertIsNone(state.inputs["option_b"]["loss_percent"])
        state.apply({"action": "edit", "patch": {"option_b": {"loss_percent": 4}}})
        self.assertEqual(state.inputs["option_b"]["loss_percent"], 4)

    def test_선택한_신규묶음에서_최신일과_규격코드를_같이_조회(self):
        # [버전 회귀] 기존 Agent 코드 역조회는 공용 가격표를 읽었기 때문에
        # 예측 입력만 새 데이터로 바꾸면 날짜·규격이 서로 달라진다.
        from agents.seunghyuk import forecast_agent as agent
        from agents.seunghyuk.forecast_bridge import EXPERIMENT_DATASET_ENV
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "daily_prices.csv"
            fields = ["date", "quality_flag", "market_code", "variety", "variety_code",
                      "grade", "grade_code", "size", "size_code", "package", "package_kg", "trade_type",
                      "avg_price_kg", "total_kg", "transaction_count"]
            row = dict(zip(fields, ["2026-11-13", "OK", "110001", "조생귤", "01", "특",
                                    "11", "소", "184", "상자", "5.000", "경매", "2500.00", "100", "5"]))
            with source.open("w", encoding="utf-8-sig", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            with patch.dict("os.environ", {EXPERIMENT_DATASET_ENV: "market-20261113"}), \
                 patch("agents.seunghyuk.forecast_agent.selected_experiment_source",
                       return_value={"price_file": source}):
                latest = agent.ExperimentalForecastDraft._resolve_date("latest_observed")
                resolution = agent._resolve_spec(SPEC, latest)
            self.assertEqual(latest.isoformat(), "2026-11-13")
            self.assertEqual(resolution["spec"]["variety_code"], "01")

    def test_표시명을_원본_코드에_근거해서_연결(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "daily_prices.csv"
            f = ["date", "quality_flag", "market_code", "variety", "variety_code", "grade", "grade_code",
                 "size", "size_code", "package", "package_kg", "trade_type"]
            row = dict(zip(f, ["2026-11-03", "OK", "110001", "조생귤", "01", "특", "11", "소", "184", "상자", "5.000", "경매"]))
            with p.open("w", encoding="utf-8-sig", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=f)
                writer.writeheader()
                writer.writerow(row)
            with patch.dict("os.environ", {"JEJU_EXPERIMENTAL_DATASET": ""}), \
                 patch("agents.seunghyuk.forecast_agent.PRICE_FILE", p):
                resolved = _resolve_spec(SPEC, __import__('datetime').date(2026, 11, 3))
            self.assertEqual(resolved["spec"]["variety_code"], "01")
            self.assertEqual(resolved["applied_data_defaults"]["market_code"], "110001")

    def test_실제_학습모델_출력은_서버에서만_숫자를_렌더링(self):
        state = ExperimentalForecastDraft()
        with patch("agents.seunghyuk.forecast_agent._resolve_spec", return_value=RESOLVED), \
             patch("agents.seunghyuk.forecast_agent.compare_experimental", return_value=deepcopy(EXAMPLE_RESULT)) as model:
            result = state.apply({"action": "new", "patch": COMPLETE})
        self.assertEqual(result["status"], "EXPERIMENTAL_ONLY")
        self.assertEqual(model.call_args.kwargs["spec"], RESOLVED["spec"])
        self.assertNotIn("provenance", model_receipt(result))
        self.assertNotIn("calculation", model_receipt(result))
        self.assertIn("995,352원", format_experimental_result(result))
        self.assertIn("정식 출하 권고 아님", format_experimental_result(result))

    def test_기존_대화Agent에_실험도구는_옵트인으로만_등록(self):
        # 실제 API를 사용하지 않고 Responses 함수 호출을 흉내 내어 라우팅을 검증한다.
        from agents.juneok.agent import AgentSession
        from agents.juneok.config import Settings

        class DummyClient:
            def __init__(self):
                self.responses = self
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(status="completed", output=[SimpleNamespace(
                    type="function_call", name="compare_experimental_forecast", call_id="mock-forecast-1",
                    arguments=json.dumps({"action": "new", "patch": COMPLETE, "finish_turn": True}, ensure_ascii=False))],
                    output_text="모델이 만든 허위가격 999999원", model="mock-model")

        client = DummyClient()
        session = AgentSession(Settings(model="mock-model"), client=client, experimental_forecast=True)
        with patch("agents.seunghyuk.forecast_agent._resolve_spec", return_value=RESOLVED), \
             patch("agents.seunghyuk.forecast_agent.compare_experimental", return_value=deepcopy(EXAMPLE_RESULT)):
            answer = session.run("2026-11-03 기준 조생귤 연구용 ML 가격 비교를 실행해 주세요")
        self.assertEqual(answer["status"], "OK", answer["events"])
        self.assertEqual(answer["visible_results"][0]["tool"], "compare_experimental_forecast")
        self.assertIn("995,352원", answer["text"])
        self.assertNotIn("999999원", answer["text"])
        self.assertIn("compare_experimental_forecast", {x["name"] for x in client.calls[0]["tools"]})
        sent_receipt = [x for x in session.messages if isinstance(x, dict) and x.get("type") == "function_call_output"]
        self.assertEqual(len(sent_receipt), 1)
        self.assertNotIn("995352", sent_receipt[0]["output"])
        self.assertNotIn("3107.78", sent_receipt[0]["output"])

        normal_client = DummyClient()
        normal = AgentSession(Settings(model="mock-model"), client=normal_client)
        normal.run("연구용 ML 가격 비교를 실행해 주세요")
        self.assertNotIn("compare_experimental_forecast", {x["name"] for x in normal_client.calls[0]["tools"]})


    # [회귀/원인] 동일한 ML 질문의 두 번째 턴에서 일반 계산 도구가 선택됐던 문제.
    # 조건 보완은 ML로, 명시적으로 다른 질문을 하면 일반 도구로 돌아가는지 검사한다.
    def test_미완료_실험은_입력보완동안_계속_선택하고_명시적_전환은_허용(self):
        from agents.seunghyuk.forecast_agent import keep_experimental_route
        draft = ExperimentalForecastDraft()
        self.assertTrue(keep_experimental_route(
            "연구용 ML 모델로 조생귤 3톤의 3일/14일 출하를 비교해줘", draft))
        result = draft.apply({"action": "new", "patch": {
            "as_of": "2026-11-03", "quantity": 3000, "spec": SPEC,
            "option_a": {"delay_days": 3}, "option_b": {"delay_days": 14}}})
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertTrue(draft.awaiting_input)
        self.assertTrue(keep_experimental_route("수수료 차감 전입니다. A 감모 1%, B 감모 4%", draft))
        self.assertTrue(keep_experimental_route(
            "연구용 ML 모델로 조생귤 3톤의 3일/14일 출하를 비교해줘", draft))
        self.assertFalse(keep_experimental_route("다른 질문: 감모율 설명해줘", draft))
        draft.reset()
        self.assertFalse(draft.awaiting_input)
        self.assertFalse(keep_experimental_route("감모율이 뭐죠?", draft))

    # [회귀/재현] ① 3톤·기간·규격만 전달 → ② 동일 질문 반복 →
    # ③ 수수료·감모·비용 보완. 모델이 action=new를 잘못 내도 기존 값을 유지해야 한다.
    # mock 응답이므로 실제 LLM의 품질까지 통과했다고 주장하지 않는다.
    def test_반복질문_및_다음턴_답변에서_일반계산기로_빠지지_않음(self):
        from agents.juneok.agent import AgentSession
        from agents.juneok.config import Settings

        class ReplyClient:
            def __init__(self):
                self.responses = self
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                i = len(self.calls)
                # 첫 턴은 기초정보만, 둘째 턴은 똑같은 질문을 반복,
                # 셋째 턴은 빠진 비용 항목에 대한 답변입니다.
                patches = [
                    {"as_of": "2026-11-03", "quantity": 3000, "spec": SPEC,
                     "option_a": {"delay_days": 3}, "option_b": {"delay_days": 14}},
                    {"as_of": "2026-11-03", "quantity": 3000, "spec": SPEC,
                     "option_a": {"delay_days": 3}, "option_b": {"delay_days": 14}},
                    {"price_basis": "gross", "option_a": OPTION_A, "option_b": OPTION_B},
                ]
                # 모델이 반복 질문에서 new로 돌려줘도 백엔드는 남은 조건을 보존해야 합니다.
                args = {"action": "new" if i < 3 else "edit",
                        "patch": patches[i - 1], "finish_turn": True}
                return SimpleNamespace(status="completed", output=[SimpleNamespace(
                    type="function_call", name="compare_experimental_forecast",
                    call_id=f"mock-forecast-{i}", arguments=json.dumps(args, ensure_ascii=False))],
                    model="mock-model", output_text="가짜 손익 999999원")

        client = ReplyClient()
        session = AgentSession(Settings(model="mock-model"), client=client,
                               experimental_forecast=True)
        repeat = "연구용 ML 모델로 조생귤 3톤의 3일 뒤와 14일 뒤 출하안을 비교해줘"
        first = session.run(repeat)
        second = session.run(repeat)
        self.assertEqual(first["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertEqual(second["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertEqual(session.experimental_forecast_draft.inputs["quantity"], 3000)
        self.assertEqual(session.experimental_forecast_draft.inputs["as_of"], "2026-11-03")
        with patch("agents.seunghyuk.forecast_agent._resolve_spec", return_value=RESOLVED), \
             patch("agents.seunghyuk.forecast_agent.compare_experimental", return_value=deepcopy(EXAMPLE_RESULT)):
            third = session.run("수수료 차감 전, A 감모 1%, B 감모 4%, 하루 보관비 각 1만원, "
                                "기타비용 각 5천원, 수수료율 A 5% B 7%")
        self.assertEqual(third["visible_results"][0]["result"]["status"], "EXPERIMENTAL_ONLY")
        self.assertIn("995,352원", third["text"])
        self.assertFalse(session.experimental_forecast_draft.awaiting_input)
        # 핵심 재발 방지 조건: 실험 입력을 보완하는 각 턴에서는
        # 모델이 일반 계산 도구를 선택할 수 없어야 한다.
        for sent in client.calls:
            self.assertEqual(sent["tool_choice"], {
                "type": "function", "name": "compare_experimental_forecast"})
            self.assertEqual([tool["name"] for tool in sent["tools"]],
                             ["compare_experimental_forecast"])

if __name__ == "__main__":
    unittest.main()
