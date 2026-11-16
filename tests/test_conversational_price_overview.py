"""Regressions for the user's price inquiry -> help -> complaint journey."""
import csv
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS

from agents.agent import AgentSession
from agents.dialogue import remember_visible_reply
from core.config import Settings
from services.price_overview import get_price_overview, render_price_overview


class Client:
    def __init__(self, *replies):
        self.responses = self
        self.replies = iter(replies)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return next(self.replies)


def answer(text):
    return NS(output=[], output_text=text, status="completed")


class DialogueTests(unittest.TestCase):
    def test_help_with_numbers_and_complaints_are_not_replaced_by_calculator(self):
        explanation = "시장코드는 모르셔도 됩니다. 예를 들어 '특 등급, 5kg 상자'처럼 말씀하시면 됩니다. 입력 형식의 예시입니다."
        client = Client(answer(explanation), answer("입력 질문은 멈출게요. 지금은 작년 시장가격이 궁금하신 것이죠."))
        agent = AgentSession(Settings(model="mock-model"), client)
        before = agent.store.snapshot()
        first = agent.run("시장코드를 제가 어떻게 알죠? 등급이랑 포장 종류의 입력 예시를 알려주세요")
        second = agent.run("아니 사용자한테 시장코드 알려달라는건 좀 아니지 않아요?")
        self.assertEqual(first["text"], explanation)
        self.assertIn({"role": "assistant", "content": explanation}, client.requests[1]["input"])
        self.assertNotIn("계산하려면", second["text"])
        self.assertEqual(second["tool_results"], [])
        self.assertEqual(agent.store.snapshot(), before)

    def test_rejected_price_does_not_trigger_a_questionnaire_or_enter_history(self):
        client = Client(answer("작년 가격은 99999999원입니다."))
        agent = AgentSession(Settings(model="mock-model"), client)
        reply = agent.run("작년 가격이 궁금해요")
        self.assertNotIn("99999999", reply["text"])
        self.assertNotIn("계산하려면", reply["text"])
        self.assertNotIn("99999999", json.dumps(agent.messages, ensure_ascii=False))
        self.assertEqual(reply["tool_results"], [])

    def test_overview_tool_synthesis_and_followup_preserve_actual_display(self):
        call = NS(type="function_call", name="get_price_overview", call_id="overview",
                  arguments=json.dumps({"start_date": "2025-01-01", "end_date": "2025-12-31", "finish_turn": False}))
        client = Client(NS(output=[call], output_text="", status="completed"),
                        answer("품종별 가격을 조회했습니다. 빠진 월이 있어 연간 전체 평균과 구분해야 합니다."),
                        answer("등급과 포장은 몰라도 시장 전체 흐름을 보실 수 있어요."))
        agent = AgentSession(Settings(model="mock-model"), client)
        result = agent.run("작년 감귤 출하 가격이 어느 정도였는지 궁금해요")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["tool_results"][0]["result"]["status"], "OK")
        self.assertIn("원/kg", result["text"])
        self.assertIn("자료 없는 월", result["text"])
        self.assertNotIn("계산하려면", result["text"])
        agent.run("등급을 모르는데요")
        self.assertIn({"role": "assistant", "content": result["text"]}, client.requests[-1]["input"])
        self.assertEqual(agent.store.version, 0)

    def test_private_local_result_is_not_replayed_as_assistant_text(self):
        messages = []
        remember_visible_reply(messages, "검산", {"status": "OK", "text": "정산액 12345678원",
            "visible_results": [{"tool": "reconcile_settlement"}], "metrics": {"model_requests": 0}})
        self.assertNotIn("12345678", json.dumps(messages, ensure_ascii=False))


class PriceOverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "data/seasons/2025-2026/daily_prices.csv"
        self.path.parent.mkdir(parents=True)
        self.rows = [self.row("2025-10-01", "조생귤", "01", 10, 1000),
                     self.row("2025-10-02", "조생귤", "01", 90, 27000),
                     self.row("2025-10-02", "하우스감귤", "03", 1, 9000)]
        self.write()

    def row(self, day, variety, code, weight, amount):
        return dict(date=day, market_code="110001", market="서울가락", variety=variety, variety_code=code,
                    grade="특", grade_code="11", size="소", size_code="1", package="상자", package_kg="5",
                    trade_type="경매", total_kg=weight, total_amount_krw=amount, transaction_count=2, quality_flag="OK")

    def write(self):
        with self.path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
            writer.writeheader()
            writer.writerows(self.rows)

    def query(self, **filters):
        return get_price_overview({"start_date": "2025-01-01", "end_date": "2025-12-31", **filters}, root=self.root)

    def test_broad_query_weights_by_kg_and_keeps_varieties_separate(self):
        result = self.query(variety="감귤")
        self.assertEqual(result["status"], "OK")
        self.assertEqual([(g["variety"], g["price_per_kg"]) for g in result["groups"]],
                         [("조생귤", "280"), ("하우스감귤", "9000")])
        self.assertEqual(result["coverage"]["observed_months"], ["2025-10"])
        self.assertIn("2025-03", result["coverage"]["missing_months"])
        self.assertEqual(result["choices"]["market"], ["서울가락"])
        self.assertIn("연간 전체 평균이 아닙니다", render_price_overview(result))

    def test_human_market_names_and_unknown_filter_offer_real_candidates(self):
        self.assertEqual(self.query(market="가락시장", package_kg=5)["status"], "OK")
        missing = self.query(grade="없는 등급")
        self.assertEqual(missing["status"], "NO_DATA")
        self.assertEqual(missing["choices"]["grade"], ["특"])

    def test_duplicate_rows_fail_without_inflating_the_aggregate(self):
        self.rows.append(self.rows[0])
        self.write()
        self.assertEqual(self.query()["status"], "DATA_ERROR")

    def test_unavailable_period_is_not_filled_from_other_dates(self):
        result = get_price_overview({"start_date": "2024-01-01", "end_date": "2024-12-31"}, root=self.root)
        self.assertEqual(result["status"], "NO_DATA")
        self.assertNotIn("groups", result)


if __name__ == "__main__":
    unittest.main()
