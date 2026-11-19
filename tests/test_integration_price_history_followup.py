"""과거 가격 조회의 명시적 후속 조건 유지·철회·격리를 검증합니다."""
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from services.price_history_followup import (
    MissingPreviousPriceQuery, resolve_price_history_query, resolve_price_history_request,
)
from agents.agent import AgentSession
from core.config import Settings
from agents.state import ScenarioStore, demo_scenario
from ui.workspace import SharedWorkspace


기준조건 = {
    "variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
    "package_kg": 5, "interval_days": 7, "limit": 3,
}


def 도구_요청(인수, 번호=1, *, 끝=True):
    return SimpleNamespace(type="function_call", name="get_price_history",
                           arguments=json.dumps({**인수, "finish_turn": 끝}, ensure_ascii=False),
                           call_id=f"price_{번호}")


def 모의_응답(인수, 번호=1, *, 끝=True):
    return SimpleNamespace(output=[도구_요청(인수, 번호, 끝=끝)], status="completed",
                           output_text="모델이 임의로 제시한 미래가격 99999999원")


class 모의_클라이언트:
    def __init__(self, 응답들):
        self.responses = self
        self.items = iter(응답들)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return next(self.items)


def 검증용_조회(조건):
    """사용자가 입력한 조건만 검사하는 모의 결과이며 실제 가격 예측은 없습니다."""
    필요한조건 = ("variety", "grade", "size", "package", "package_kg", "interval_days")
    부족 = [name for name in 필요한조건 if 조건.get(name) is None]
    공통 = {"analysis_type": "price_history", "is_future_forecast": False,
            "cautions": ["미래가격 예측이 아닙니다."]}
    if 부족:
        return {**공통, "status": "NEEDS_INPUT", "source_status": "NEEDS_CLARIFICATION",
                "missing_fields": 부족, "choices": {"grade": ["특", "상"], "interval_days": [3, 7, 14]},
                "message": "필요한 과거 비교 조건을 알려주세요."}
    return {**공통, "status": "OK", "source_status": "OK",
            "conditions": {k: str(v) for k, v in 조건.items() if k != "limit"},
            "interval_days": 조건["interval_days"], "clean_pairs": 1,
            "warning_pairs": 0, "unavailable_pairs": 0,
            "clean_change_range_pct": {"min": "-25.85", "max": "-25.85"},
            "examples": [{"base_date": "2026-10-02", "target_date": "2026-10-09",
                          "base_price_kg": "3101.12", "target_price_kg": "2299.30",
                          "price_change_pct": "-25.85"}],
            "source": "data/processed/price_comparison_availability.csv",
            "applied_defaults": {"market_code": "110001", "trade_type": "경매"},
            "applied_defaults_note": "시장: 서울가락 거래방식: 경매"}


class 가격조회후속조건검수(unittest.TestCase):
    def _session(self, *calls):
        client = 모의_클라이언트([모의_응답(args, i + 1) for i, args in enumerate(calls)])
        session = AgentSession(Settings(model="mock-model"), client)
        session.store = ScenarioStore(demo_scenario())
        return session, client

    def test_간격만_바꾸면_명시한_규격을_유지(self):
        actual = resolve_price_history_query({"follow_up": True, "interval_days": 14}, 기준조건)
        self.assertEqual(actual["interval_days"], 14)
        for field in ("variety", "grade", "size", "package", "package_kg", "limit"):
            self.assertEqual(actual[field], 기준조건[field])
        self.assertNotIn("market_code", actual)
        self.assertNotIn("trade_type", actual)
        self.assertEqual(기준조건["interval_days"], 7)

    def test_새조회에서_기존_조건을_사용하지_않음(self):
        actual = resolve_price_history_query({"follow_up": False, "variety": "하우스감귤",
                                              "interval_days": 3}, 기준조건)
        self.assertEqual(actual, {"variety": "하우스감귤", "interval_days": 3, "limit": 3})

    def test_사용자_직접_입력한_기준일을_유지하고_철회(self):
        previous = {**기준조건, "base_date": "2026-10-02"}
        kept = resolve_price_history_query({"follow_up": True, "interval_days": 14}, previous)
        self.assertEqual(kept["base_date"], "2026-10-02")
        cleared = resolve_price_history_query({"follow_up": True, "clear_fields": ["base_date"]}, kept)
        self.assertNotIn("base_date", cleared)
        self.assertEqual(cleared["interval_days"], 14)

    def test_후속조회에_기존입력이_없으면_거부(self):
        with self.assertRaises(MissingPreviousPriceQuery):
            resolve_price_history_query({"follow_up": True, "interval_days": 14}, None)
        with self.assertRaises(MissingPreviousPriceQuery):
            resolve_price_history_query({"follow_up": True, "interval_days": 14}, {"limit": 3})

    def test_잘못된_철회와_유효하지_않은_값을_거부(self):
        invalid = [
            {"follow_up": True, "grade": "상", "clear_fields": ["grade"]},
            {"follow_up": True, "clear_fields": ["grade", "grade"]},
            {"follow_up": False, "clear_fields": ["grade"]},
            {"follow_up": 1}, {"follow_up": True, "interval_days": 0},
            {"follow_up": True, "package_kg": True},
            {"follow_up": True, "extra_field": "값"},
            {"follow_up": True, "package_kg": float("nan")},
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                resolve_price_history_query(args, 기준조건)

    def test_대화의_7일에서_14일로_실제로_승계(self):
        session, client = self._session(기준조건, {"follow_up": True, "interval_days": 14})
        calls = []
        def query(args):
            calls.append(deepcopy(args))
            return 검증용_조회(args)
        with patch("services.price_history_service.get_integrated_price_history", side_effect=query):
            a = session.run("조생귤 특 소 상자 5kg의 7일 과거 가격을 알려줘")
            b = session.run("그럼 나머지는 유지하고 14일로 바꿔줘")
        self.assertEqual((a["status"], b["status"]), ("OK", "OK"))
        self.assertEqual([x["interval_days"] for x in calls], [7, 14])
        self.assertEqual(calls[1]["variety"], "조생귤")
        self.assertEqual(calls[1]["grade"], "특")
        self.assertEqual(calls[1]["package_kg"], 5)
        self.assertEqual(session.price_history_query, calls[1])
        self.assertEqual(len(client.requests), 2)
        self.assertIn('"interval_days": 7', str(client.requests[1]["input"]))
        self.assertNotIn("99999999", b["text"])

    def test_같은_턴에_승계_표기만_다르면_조회와_표시를_재사용한다(self):
        client = 모의_클라이언트([
            모의_응답(기준조건, 끝=False),
            모의_응답({"follow_up": True, "package_kg": "5.0"}, 2),
        ])
        session = AgentSession(Settings(model="mock-model"), client)
        with patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회) as fetch:
            reply = session.run("조생귤 특 소 상자 5kg의 과거 7일 경락가격을 비교해 줘")
        fetch.assert_called_once_with(기준조건)
        self.assertEqual(reply["status"], "OK")
        self.assertEqual(len(reply["tool_results"]), 1)
        self.assertEqual(len(reply["visible_results"]), 1)
        self.assertEqual(reply["termination"]["tool_calls_executed"], 1)
        self.assertEqual(reply["metrics"]["reused_price_history_calls"], ["price_2"])
        receipt = next(m for m in session.messages if isinstance(m, dict) and m.get("call_id") == "price_2")
        self.assertTrue(json.loads(receipt["output"])["reused_same_turn"])

    def test_중복_다음에_다른_간격과_기준일은_별도로_조회한다(self):
        client = 모의_클라이언트([
            모의_응답(기준조건, 끝=False),
            모의_응답({"follow_up": True}, 2, 끝=False),
            모의_응답({"follow_up": True, "interval_days": 14}, 3, 끝=False),
            모의_응답({"follow_up": True, "base_date": "2026-10-02"}, 4),
        ])
        session = AgentSession(Settings(model="mock-model"), client)
        with patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회) as fetch:
            reply = session.run("같은 규격 7일과 14일 가격을 보고, 14일은 10월 2일 기준도 따로 조회해 줘")
        actual = [(item.args[0]["interval_days"], item.args[0].get("base_date")) for item in fetch.call_args_list]
        self.assertEqual(actual, [(7, None), (14, None), (14, "2026-10-02")])
        self.assertEqual(len(reply["visible_results"]), 3)

    def test_명시한_축약_크기를_복원하고_불확실한_크기는_선택하지_않는다(self):
        from services.price_history_followup import resolve_price_history_request

        supplied = {**기준조건, "size": None}
        for question, size in (("조생귤 특 소 상자 5kg의 과거 7일 가격", "소"),
                               ("조생귤 특 소 상자인지 헷갈려요", None),
                               ("특 소 상자 말고 중과를 봐줘", None),
                               ("특 소 상자와 특 중 상자의 가격", None)):
            with self.subTest(question=question):
                query, _ = resolve_price_history_request(supplied, None, question=question)
                self.assertEqual(query.get("size"), size)

    def test_같은_규격의_간격을_여러_개_말하면_각각_한_번_조회한다(self):
        session, client = self._session(기준조건)
        with patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회) as fetch:
            session.run("조생귤 특 소 상자 5kg의 7일 가격을 알려줘")
            fetch.reset_mock()
            reply = session.run("같은 규격으로 과거 7일 간격과 14일 간격의 경락가격을 둘 다 비교해 줘.")
        self.assertEqual([item.args[0]["interval_days"] for item in fetch.call_args_list], [7, 14])
        self.assertEqual([item["result"]["interval_days"] for item in reply["visible_results"]], [7, 14])
        self.assertEqual(len(client.requests), 1)

    def test_가격_간격과_보관_기간_또는_시장_변경을_섞지_않는다(self):
        from services.price_history_followup import requested_same_spec_intervals

        self.assertEqual(requested_same_spec_intervals("같은 규격으로 과거 경락가격 7일·14일·7일 비교해 줘"), (7, 14))
        for question in ("같은 규격으로 과거 가격 7일과 14일, 시장은 부산으로",
                         "같은 규격으로 과거 가격을 참고해서 보관 7일과 14일 손익을 비교해 줘",
                         "같은 규격으로 과거 가격을 7일과 14일 중 하나만 보고 싶어요"):
            with self.subTest(question=question):
                self.assertEqual(requested_same_spec_intervals(question), ())

    def test_가격_자료_지문이_바뀌면_재사용하지_않는다(self):
        client = 모의_클라이언트([
            모의_응답(기준조건, 끝=False),
            모의_응답({"follow_up": True}, 2),
        ])
        session = AgentSession(Settings(model="mock-model"), client)
        with patch("services.future_pair.price_revision", side_effect=[("old",)] * 2 + [("new",)] * 3), \
             patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회) as fetch:
            reply = session.run("과거 7일 경락가격을 비교해 줘")
        self.assertEqual(fetch.call_count, 2)
        self.assertNotIn("reused_price_history_calls", reply["metrics"])

    def test_누락된_조건도_후속_응답으로_채울_수_있음(self):
        session, _ = self._session({"variety": "조생귤"},
                                   {"follow_up": True, "grade": "특", "size": "소", "package": "상자",
                                    "package_kg": 5, "interval_days": 7})
        seen = []
        def query(args):
            seen.append(deepcopy(args))
            return 검증용_조회(args)
        with patch("services.price_history_service.get_integrated_price_history", side_effect=query):
            first = session.run("조생귤의 과거 가격을 알려줘")
            second = session.run("특, 소, 상자 5kg, 7일로 조회해줘")
        self.assertEqual(first["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertEqual(second["visible_results"][0]["result"]["status"], "OK")
        self.assertEqual(seen[1]["variety"], "조생귤")
        self.assertEqual(seen[1]["interval_days"], 7)

    def test_통칭_품종만_재질문하고_명시한_규격은_승계(self):
        session, _ = self._session({**기준조건, "variety": "감귤"},
                                   {"follow_up": True, "variety": "조생귤"})
        first = session.run("감귤 특 소 상자 5kg의 과거 7일 가격을 알려줘")
        result = first["visible_results"][0]["result"]
        self.assertEqual(result["missing_fields"], ["variety"])
        self.assertEqual(session.price_history_query["grade"], "특")
        self.assertEqual(session.price_history_query["size"], "소")
        second = session.run("조생귤이에요.")
        self.assertEqual(second["visible_results"][0]["result"]["clean_pairs"], 20)

    def test_포장중량_충돌시_조회_보류와_정정_후_재개(self):
        session, _ = self._session(
            기준조건,
            {"follow_up": True, "package_kg": 10, "conflicting_package_kg": [10, 5]},
            {"follow_up": True, "interval_days": 14},
            {"follow_up": True, "package_kg": 5},
        )
        scenario = session.store.snapshot()
        with patch("services.price_history_service.get_integrated_price_history",
                   side_effect=검증용_조회) as lookup:
            session.run("조생귤 특 소 상자 5kg의 과거 7일 가격을 알려줘")
            conflict = session.run("포장 표기는 10kg인데 한 상자가 5kg라고 들었어요.")
            result = conflict["visible_results"][0]["result"]
            self.assertEqual(result["source_status"], "PACKAGE_WEIGHT_CONFLICT")
            self.assertEqual(result["missing_fields"], ["package_kg"])
            self.assertIn("10kg / 5kg", conflict["text"])
            self.assertIn("10kg / 5kg", conflict["report"])
            self.assertNotIn("package_kg", session.price_history_query)
            self.assertEqual(lookup.call_count, 1)

            pending = session.run("조회 간격만 14일로 바꿔줘")
            self.assertEqual(pending["visible_results"][0]["result"]["missing_fields"], ["package_kg"])
            resolved = session.run("한 상자 5kg이 맞아요.")
            self.assertEqual(resolved["visible_results"][0]["result"]["status"], "OK")
            self.assertEqual(lookup.call_args.args[0]["package_kg"], 5)
            self.assertEqual(lookup.call_args.args[0]["interval_days"], 14)
        self.assertEqual(session.store.snapshot(), scenario)

    def test_포장명_중량과_별도입력_충돌을_파이썬에서도_차단(self):
        from services.price_history_followup import resolve_price_history_request
        args = {**기준조건, "package": "10kg 박스", "package_kg": 5}
        pending, clarification = resolve_price_history_request(args, None)
        self.assertEqual(clarification["source_status"], "PACKAGE_WEIGHT_CONFLICT")
        self.assertEqual(clarification["missing_fields"], ["package_kg"])
        self.assertEqual(pending["package"], "박스")
        self.assertNotIn("package_kg", pending)
        # 확인된 중량만 보충하면 과거 문구의 10kg 때문에 재차 충돌하면 안 됩니다.
        corrected, next_clarification = resolve_price_history_request(
            {"follow_up": True, "package_kg": 5}, pending)
        self.assertIsNone(next_clarification)
        self.assertEqual(corrected["package"], "박스")
        self.assertEqual(corrected["package_kg"], 5)
        self.assertEqual(corrected["interval_days"], 7)

    def test_포장명에_중량만_있으면_포장종류는_자동추측하지_않음(self):
        from services.price_history_followup import resolve_price_history_request
        pending, clarification = resolve_price_history_request(
            {**기준조건, "package": "10kg", "package_kg": 5}, None)
        self.assertEqual(clarification["source_status"], "PACKAGE_WEIGHT_CONFLICT")
        self.assertNotIn("package", pending)
        self.assertNotIn("package_kg", pending)

    def test_유일한_시장과_거래방식_자동값은_기억하지_않음(self):
        session, _ = self._session(기준조건)
        with patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("조생귤 특 소 상자 5kg, 7일")
        self.assertNotIn("market_code", session.price_history_query)
        self.assertNotIn("trade_type", session.price_history_query)
        self.assertNotIn("base_date", session.price_history_query)

    def test_가격_표현과_모델의_추측을_거래방식으로_보내지_않음(self):
        for word, guessed in (("경락가격", "경락가격"), ("경매가격", "경매가격"),
                              ("경매 가격", "경매"), ("시세", "정가수의")):
            with self.subTest(word=word, guessed=guessed):
                session, _ = self._session({**기준조건, "trade_type": guessed})
                with patch("services.price_history_service.get_integrated_price_history",
                           side_effect=검증용_조회) as lookup:
                    reply = session.run(f"조생귤 특 소 상자 5kg의 과거 7일 {word}을 조회해 줘.")
                lookup.assert_called_once_with(기준조건)
                self.assertEqual(reply["visible_results"][0]["result"]["status"], "OK")
                self.assertNotIn("trade_type", session.price_history_query)

    def test_명시한_거래방식만_저장하고_후속_간격_변경에_유지함(self):
        for phrase, expected in (("경매 방식으로", "경매"), ("경매만", "경매"),
                                 ("거래방식은 경매로", "경매"), ("정가 수의 매매로", "정가수의"),
                                 ("경매 방식 말고 정가·수의로", "정가수의")):
            with self.subTest(phrase=phrase, expected=expected):
                current, _ = resolve_price_history_request(
                    {**기준조건, "trade_type": "경락가격"}, None, question=f"{phrase} 시세를 확인해 줘.")
                self.assertEqual(current["trade_type"], expected)
                follow_up, _ = resolve_price_history_request(
                    {"follow_up": True, "interval_days": 14, "trade_type": "경매가격"},
                    current, question="14일 경매가격을 비교해 줘.")
                self.assertEqual(follow_up["trade_type"], expected)
                self.assertEqual(follow_up["interval_days"], 14)

    def test_철회하면_기존_등급을_다시_사용하지_않음(self):
        session, _ = self._session(기준조건, {"follow_up": True, "clear_fields": ["grade"]})
        with patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("조생귤 특 소 상자 5kg, 7일")
            reply = session.run("등급은 아직 모르겠으니 빼줘")
        self.assertNotIn("grade", session.price_history_query)
        self.assertEqual(reply["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertIn("등급", reply["text"])

    def test_후속_조회_조건이_없을때_시장조회로_자동보충하지_않음(self):
        session, _ = self._session({"follow_up": True, "interval_days": 14})
        session.task_context.value["market_query"] = {"variety": "조생귤", "grade": "특"}
        with patch("services.price_history_service.get_integrated_price_history") as fetch:
            reply = session.run("전에 봤던 가격을 14일로 바꿔줘")
        fetch.assert_not_called()
        self.assertEqual(reply["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertEqual(reply["visible_results"][0]["result"]["missing_fields"], ["previous_price_history"])
        self.assertIsNone(session.price_history_query)

    def test_과거_가격간격과_보관일은_다른_값(self):
        session, _ = self._session(기준조건, {"follow_up": True, "interval_days": 14})
        scenario = session.store.snapshot()
        with patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("7일간 과거 경락가격을 비교해줘")
            session.run("14일 과거 가격으로 바꿔줘")
        self.assertEqual(session.store.snapshot(), scenario)
        self.assertEqual(session.store.scenario["options"][0]["delay_days"], 7)
        self.assertEqual(session.price_history_query["interval_days"], 14)

    def test_자료_버전이_바뀌면_이전조건을_폐기(self):
        session, _ = self._session(기준조건, {"follow_up": True, "interval_days": 14})
        with patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("이전 가격 비교")
        with patch("agents.agent.data_revision", return_value=("new-data",)), \
             patch("services.price_history_service.get_integrated_price_history") as fetch:
            reply = session.run("14일로 바꿔줘")
        fetch.assert_not_called()
        self.assertEqual(reply["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertIsNone(session.price_history_query)

    def test_대화_초기화는_과거가격_조건도_초기화(self):
        session, _ = self._session(기준조건)
        ws = SharedWorkspace(session)
        with patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("7일 비교")
        self.assertIsNotNone(session.price_history_query)
        ws.clear_conversation(preserve_conditions=True)
        self.assertIsNone(session.price_history_query)
        self.assertIsNone(session.price_history_revision)

    def test_모델에_가격_원본을_전달하지_않음(self):
        session, client = self._session(기준조건, {"follow_up": True, "interval_days": 14})
        with patch("services.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("7일 가격비교")
            session.run("14일로 변경")
        for request in client.requests:
            contents = json.dumps(request, ensure_ascii=False, default=str)
            self.assertNotIn("2299.30", contents)
            self.assertNotIn("3101.12", contents)
        receipt = [m["output"] for m in session.messages if isinstance(m, dict) and m.get("type") == "function_call_output"]
        self.assertEqual(len(receipt), 2)
        self.assertNotIn("2299.30", " ".join(receipt))


if __name__ == "__main__":
    unittest.main()
