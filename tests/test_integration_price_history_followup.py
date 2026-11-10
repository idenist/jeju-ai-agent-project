"""과거 가격 조회의 명시적 후속 조건 유지·철회·격리를 검증합니다."""
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from agents.common.price_history_followup import (
    MissingPreviousPriceQuery, resolve_price_history_query,
)
from agents.juneok.agent import AgentSession
from agents.juneok.config import Settings
from agents.juneok.state import ScenarioStore, demo_scenario
from agents.juneok.workspace import SharedWorkspace


기준조건 = {
    "variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
    "package_kg": 5, "interval_days": 7, "limit": 3,
}


def 도구_요청(인수, 번호=1):
    return SimpleNamespace(type="function_call", name="get_price_history",
                           arguments=json.dumps({**인수, "finish_turn": True}, ensure_ascii=False),
                           call_id=f"price_{번호}")


def 모의_응답(인수, 번호=1):
    return SimpleNamespace(output=[도구_요청(인수, 번호)], status="completed",
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
        with patch("agents.common.price_history_service.get_integrated_price_history", side_effect=query):
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

    def test_누락된_조건도_후속_응답으로_채울_수_있음(self):
        session, _ = self._session({"variety": "조생귤"},
                                   {"follow_up": True, "grade": "특", "size": "소", "package": "상자",
                                    "package_kg": 5, "interval_days": 7})
        seen = []
        def query(args):
            seen.append(deepcopy(args))
            return 검증용_조회(args)
        with patch("agents.common.price_history_service.get_integrated_price_history", side_effect=query):
            first = session.run("조생귤의 과거 가격을 알려줘")
            second = session.run("특, 소, 상자 5kg, 7일로 조회해줘")
        self.assertEqual(first["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertEqual(second["visible_results"][0]["result"]["status"], "OK")
        self.assertEqual(seen[1]["variety"], "조생귤")
        self.assertEqual(seen[1]["interval_days"], 7)

    def test_유일한_시장과_거래방식_자동값은_기억하지_않음(self):
        session, _ = self._session(기준조건)
        with patch("agents.common.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("조생귤 특 소 상자 5kg, 7일")
        self.assertNotIn("market_code", session.price_history_query)
        self.assertNotIn("trade_type", session.price_history_query)
        self.assertNotIn("base_date", session.price_history_query)

    def test_철회하면_기존_등급을_다시_사용하지_않음(self):
        session, _ = self._session(기준조건, {"follow_up": True, "clear_fields": ["grade"]})
        with patch("agents.common.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("조생귤 특 소 상자 5kg, 7일")
            reply = session.run("등급은 아직 모르겠으니 빼줘")
        self.assertNotIn("grade", session.price_history_query)
        self.assertEqual(reply["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertIn("등급", reply["text"])

    def test_후속_조회_조건이_없을때_시장조회로_자동보충하지_않음(self):
        session, _ = self._session({"follow_up": True, "interval_days": 14})
        session.task_context.value["market_query"] = {"variety": "조생귤", "grade": "특"}
        with patch("agents.common.price_history_service.get_integrated_price_history") as fetch:
            reply = session.run("전에 봤던 가격을 14일로 바꿔줘")
        fetch.assert_not_called()
        self.assertEqual(reply["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertEqual(reply["visible_results"][0]["result"]["missing_fields"], ["previous_price_history"])
        self.assertIsNone(session.price_history_query)

    def test_과거_가격간격과_보관일은_다른_값(self):
        session, _ = self._session(기준조건, {"follow_up": True, "interval_days": 14})
        scenario = session.store.snapshot()
        with patch("agents.common.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("7일간 과거 경락가격을 비교해줘")
            session.run("14일 과거 가격으로 바꿔줘")
        self.assertEqual(session.store.snapshot(), scenario)
        self.assertEqual(session.store.scenario["options"][0]["delay_days"], 7)
        self.assertEqual(session.price_history_query["interval_days"], 14)

    def test_자료_버전이_바뀌면_이전조건을_폐기(self):
        session, _ = self._session(기준조건, {"follow_up": True, "interval_days": 14})
        with patch("agents.common.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("이전 가격 비교")
        with patch("agents.juneok.agent.data_revision", return_value=("new-data",)), \
             patch("agents.common.price_history_service.get_integrated_price_history") as fetch:
            reply = session.run("14일로 바꿔줘")
        fetch.assert_not_called()
        self.assertEqual(reply["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        self.assertIsNone(session.price_history_query)

    def test_대화_초기화는_과거가격_조건도_초기화(self):
        session, _ = self._session(기준조건)
        ws = SharedWorkspace(session)
        with patch("agents.common.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
            session.run("7일 비교")
        self.assertIsNotNone(session.price_history_query)
        ws.clear_conversation(preserve_conditions=True)
        self.assertIsNone(session.price_history_query)
        self.assertIsNone(session.price_history_revision)

    def test_모델에_가격_원본을_전달하지_않음(self):
        session, client = self._session(기준조건, {"follow_up": True, "interval_days": 14})
        with patch("agents.common.price_history_service.get_integrated_price_history", side_effect=검증용_조회):
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
