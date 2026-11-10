import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from agents.jeonghyeon.agent import (PROFIT_NOTE, SCENARIO_PROPERTIES, SCENARIO_TOOL_NAME, TOOL_NAME,
                                     resolve_sale_dates, run_turn)
from agents.jeonghyeon.price_history import InputError

TODAY = date(2026, 11, 10)
PLAN = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": 5,
        "option_a_days_from_today": 0, "option_a_price_per_kg": 2500, "days_after_option_a": 7}


def args(**fields):
    row = dict.fromkeys(SCENARIO_PROPERTIES)
    row["asks_profit"] = False
    row.update(fields)
    return row


def fake_client(name, arguments, status="completed"):
    call = SimpleNamespace(type="function_call", name=name, arguments=json.dumps(arguments, ensure_ascii=False))
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(status=status, output=[call])
    return client


def turn(**fields):
    client = fake_client(SCENARIO_TOOL_NAME, args(**fields))
    return run_turn(client, [{"role": "user", "content": "7일 뒤에 팔면?"}], model="test", today=TODAY)


class ScenarioAgentTest(unittest.TestCase):
    def test_offers_both_tools_and_lets_model_choose(self):
        client = fake_client(SCENARIO_TOOL_NAME, args(**PLAN))
        run_turn(client, [{"role": "user", "content": "q"}], model="test", today=TODAY)
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["tool_choice"], "required")
        self.assertEqual({t["name"] for t in kwargs["tools"]}, {TOOL_NAME, SCENARIO_TOOL_NAME})
        self.assertFalse(kwargs["parallel_tool_calls"])

    def test_seven_day_plan_shows_assumed_prices_from_python(self):
        reply = turn(**PLAN)
        self.assertIn("A안 2026-11-10 판매 · 2,500.00원/kg (사용자 입력 가정)", reply)
        self.assertIn("B안 2026-11-17 판매 · A안보다 7일 뒤", reply)
        self.assertIn("중앙값 적용 -3.63% → B안 2,409.37원/kg [가정]", reply)
        self.assertIn("가격 전망이 아닙니다", reply)
        self.assertNotIn(PROFIT_NOTE, reply)

    def test_follow_up_changes_only_b_timing(self):
        reply = turn(**{**PLAN, "days_after_option_a": 14})
        self.assertIn("2,621.76", reply)
        self.assertIn("A안 2026-11-10 판매 · 2,500.00원/kg", reply)
        self.assertNotIn("2,409.37", reply)

    def test_unsupported_gap_leaves_no_previous_price(self):
        reply = turn(**{**PLAN, "days_after_option_a": 10})
        self.assertIn("3일, 7일, 14일", reply)
        self.assertNotIn("[가정]", reply)
        self.assertNotIn("2,409.37", reply)

    def test_missing_a_price_shows_changes_and_asks(self):
        reply = turn(**{**PLAN, "option_a_price_per_kg": None})
        self.assertIn("-3.63%", reply)
        self.assertIn("A안 단가(원/kg)를 알려 주세요", reply)
        self.assertNotIn("[가정]", reply)

    def test_missing_b_timing_asks_how_many_days_later(self):
        reply = turn(**{**PLAN, "days_after_option_a": None})
        self.assertIn("A안보다 며칠 뒤인지", reply)
        self.assertNotIn("[가정]", reply)

    def test_missing_spec_asks_before_prices(self):
        reply = turn(variety="조생귤", option_a_price_per_kg=2500, days_after_option_a=7)
        self.assertIn("다음 조건을 알려 주세요", reply)
        self.assertIn("A안 판매일을 따로 말하지 않아 오늘(2026-11-10)로", reply)

    def test_profit_question_explains_scope(self):
        self.assertIn(PROFIT_NOTE, turn(**PLAN, asks_profit=True))

    def test_both_future_dates_use_gap_only(self):
        reply = turn(**{**PLAN, "option_a_days_from_today": 3})
        self.assertIn("A안 2026-11-13", reply)
        self.assertIn("B안 2026-11-20", reply)
        self.assertIn("2,409.37", reply)

    def test_both_days_from_today_are_not_subtracted_by_model(self):
        # 모델은 3과 10을 그대로 넘기고, 간격 7일은 Python이 두 판매일의 차이로 만든다.
        reply = turn(**{**PLAN, "option_a_days_from_today": 3, "days_after_option_a": None,
                        "option_b_days_from_today": 10})
        self.assertIn("A안 2026-11-13", reply)
        self.assertIn("B안 2026-11-20 판매 · A안보다 7일 뒤", reply)
        self.assertIn("2,409.37", reply)
        a_day, b_day, _ = resolve_sale_dates({"option_a_days_from_today": 3, "option_b_days_from_today": 13}, TODAY)
        self.assertEqual((b_day - a_day).days, 10)

    def test_same_b_day_from_two_bases_is_accepted(self):
        reply = turn(**{**PLAN, "option_b_days_from_today": 7})
        self.assertIn("A안보다 7일 뒤", reply)
        self.assertIn("두 가지로 지정", turn(**{**PLAN, "option_a_days_from_today": 3,
                                              "option_b_days_from_today": 7}))

    def test_explicit_dates_and_month_day(self):
        reply = turn(**{**PLAN, "option_a_days_from_today": None, "days_after_option_a": None,
                        "option_a_sale_date": "11-20", "option_b_sale_date": "2026-12-04"})
        self.assertIn("A안보다 14일 뒤", reply)
        self.assertEqual(resolve_sale_dates({"option_a_sale_date": "01-05"}, TODAY)[0], date(2027, 1, 5))

    def test_bad_or_conflicting_dates_are_input_errors(self):
        with self.assertRaises(InputError):
            resolve_sale_dates({"option_a_sale_date": "02-30"}, TODAY)
        with self.assertRaises(InputError):
            resolve_sale_dates({"option_a_days_from_today": 0, "option_a_sale_date": "2026-11-12"}, TODAY)
        reply = turn(**{**PLAN, "option_b_sale_date": "2026-11-30"})
        self.assertIn("두 가지로 지정", reply)
        self.assertIn("1~366", turn(**{**PLAN, "days_after_option_a": 0}))

    def test_wrong_tool_or_keys_do_not_run(self):
        client = fake_client("unknown", args(**PLAN))
        self.assertIn("다시 질문", run_turn(client, [], model="test", today=TODAY))
        client = fake_client(SCENARIO_TOOL_NAME, {k: v for k, v in args(**PLAN).items() if k != "asks_profit"})
        self.assertIn("다시 질문", run_turn(client, [], model="test", today=TODAY))


if __name__ == "__main__":
    unittest.main()
