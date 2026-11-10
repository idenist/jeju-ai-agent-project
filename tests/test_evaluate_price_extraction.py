import json
import unittest
from types import SimpleNamespace

from agents.jeonghyeon.agent import SCENARIO_PROPERTIES
from scripts.evaluate_price_extraction import (CASE_FILE, PROPERTIES, SCENARIO_CASE_FILE, SCENARIO_TOOL_NAME,
                                               TOOL_NAME, add_effective_scores, evaluate, score)


def full(**fields):
    row = dict.fromkeys(PROPERTIES)
    row.update(fields)
    return row


SCENARIO_FIELDS = SCENARIO_PROPERTIES
BASE = full(intent="history", variety="조생귤", grade="특", size="소",
            package="상자", package_kg=5, interval_days=7)


def scripted_client(replies):
    # 턴마다 정해 둔 호출 인자를 돌려주는 가짜 모델
    queue = iter(replies)

    def create(**_):
        call = SimpleNamespace(type="function_call", name=TOOL_NAME,
                               arguments=json.dumps(next(queue), ensure_ascii=False))
        return SimpleNamespace(status="completed", output=[call])

    return SimpleNamespace(responses=SimpleNamespace(create=create))


class ScoreTest(unittest.TestCase):
    def test_replay_scores_normalized_and_raw_arguments_separately(self):
        actual = {**BASE, "grade": "특등급", "size": "소과"}
        changed = {**BASE, "interval_days": 14}
        follow_up = {**actual, "interval_days": 14}
        report = add_effective_scores({"exact_matches": 0, "results": [
            {"case": "c", "expected": BASE, "actual": actual, **score(BASE, actual, None)},
            {"case": "c", "expected": changed, "actual": follow_up, **score(changed, follow_up, BASE)},
        ]})
        row = report["results"][0]
        self.assertEqual(report["exact_matches"], 0)
        self.assertEqual(report["effective_exact_matches"], 2)
        self.assertEqual(report["results"][1]["dropped_fields"], ["grade", "size"])
        self.assertEqual(report["effective_turns_with_dropped_fields"], 0)
        self.assertEqual(row["effective_actual"], BASE)
        self.assertEqual(row["actual"]["grade"], "특등급")

    def test_invented_field(self):
        result = score(full(intent="history", variety="조생귤"),
                       full(intent="history", variety="조생귤", grade="특"), None)
        self.assertEqual(result["invented_fields"], ["grade"])

    def test_missed_change(self):
        result = score({**BASE, "package_kg": 3}, BASE, BASE)
        self.assertEqual(result["missed_changes"], ["package_kg"])
        self.assertEqual(result["dropped_fields"], [])

    def test_dropped_kept_condition(self):
        result = score({**BASE, "interval_days": 14},
                       {**BASE, "interval_days": 14, "grade": None}, BASE)
        self.assertEqual(result["dropped_fields"], ["grade"])
        self.assertEqual(result["missed_changes"], [])

    def test_unsupported_scores_intent_only(self):
        result = score(full(intent="unsupported"), full(intent="unsupported", variety="조생귤"), None)
        self.assertTrue(result["exact_match"])

    def test_bool_is_not_number(self):
        self.assertFalse(score({**BASE, "package_kg": 1}, {**BASE, "package_kg": True}, None)["exact_match"])

    def test_follow_up_turn_counts_dropped_field(self):
        cases = [{"id": "c", "turns": [
            {"question": "조생귤 특 소 상자 5kg 7일", "expected": {k: v for k, v in BASE.items() if v is not None}},
            {"question": "간격만 14일로", "expected": {**{k: v for k, v in BASE.items() if v is not None},
                                                   "interval_days": 14}},
        ]}]
        report = evaluate(cases, scripted_client([BASE, {**BASE, "interval_days": 14, "size": None}]), "test")
        self.assertEqual(report["turns_with_dropped_fields"], 1)
        self.assertEqual(report["exact_matches"], 1)


class ToolChoiceTest(unittest.TestCase):
    plan = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": 5,
            "option_a_price_per_kg": 2500, "days_after_option_a": 7}

    def test_case_files_pass_with_correct_calls(self):
        for path in (CASE_FILE, SCENARIO_CASE_FILE):
            report = evaluate(json.loads(path.read_text(encoding="utf-8")), None, "test", dry_run=True)
            self.assertEqual(report["exact_matches"], report["turns"], path.name)
            self.assertEqual((report["tool_mismatches"], report["reply_check_failures"]), (0, 0), path.name)

    def test_wrong_tool_is_counted_without_field_scores(self):
        cases = [{"id": "c", "turns": [{"question": "A안 2,500원이면 7일 뒤 B안은?",
                                        "tool": SCENARIO_TOOL_NAME, "expected": self.plan}]}]
        report = add_effective_scores(evaluate(cases, scripted_client([BASE]), "test"))
        row = report["results"][0]
        self.assertEqual((report["tool_mismatches"], report["exact_matches"]), (1, 0))
        self.assertEqual(row["differences"], {"tool": {"expected": SCENARIO_TOOL_NAME, "actual": TOOL_NAME}})
        self.assertFalse(row["effective_exact_match"])

    def test_reply_check_catches_leftover_price(self):
        turn = {"question": "10일 뒤면?", "tool": SCENARIO_TOOL_NAME,
                "expected": {**self.plan, "days_after_option_a": 10},
                "reply_excludes": ["2,409.37"], "reply_includes": ["3일, 7일, 14일"]}
        # 모델이 간격 변경을 놓치고 7일을 그대로 보내면 이전 가격이 다시 나온다.
        call = SimpleNamespace(type="function_call", name=SCENARIO_TOOL_NAME, arguments=json.dumps(
            {**dict.fromkeys(SCENARIO_FIELDS), "asks_profit": False, **self.plan}, ensure_ascii=False))
        client = SimpleNamespace(responses=SimpleNamespace(
            create=lambda **_: SimpleNamespace(status="completed", output=[call])))
        report = evaluate([{"id": "c", "turns": [turn]}], client, "test")
        row = report["results"][0]
        self.assertEqual(report["reply_check_failures"], 1)
        self.assertEqual((row["reply_missing"], row["reply_leaked"]), (["3일, 7일, 14일"], ["2,409.37"]))
        self.assertEqual(row["differences"]["days_after_option_a"], {"expected": 10, "actual": 7})

    def test_unstated_a_day_counts_as_today_after_normalization(self):
        expected = {**self.plan, "option_a_days_from_today": 0}
        cases = [{"id": "c", "turns": [{"question": "q", "tool": SCENARIO_TOOL_NAME, "expected": expected}]}]
        actual = {**dict.fromkeys(SCENARIO_FIELDS), "asks_profit": False, **self.plan}
        call = SimpleNamespace(type="function_call", name=SCENARIO_TOOL_NAME,
                               arguments=json.dumps(actual, ensure_ascii=False))
        client = SimpleNamespace(responses=SimpleNamespace(
            create=lambda **_: SimpleNamespace(status="completed", output=[call])))
        report = add_effective_scores(evaluate(cases, client, "test"))
        self.assertEqual((report["exact_matches"], report["effective_exact_matches"]), (0, 1))

    def test_same_sale_day_from_different_bases_matches_after_normalization(self):
        # '지금 안 팔고 7일 뒤'를 A안 기준(7)으로 받든 오늘 기준(7)으로 받든 판매일은 같다.
        expected = {**self.plan, "option_a_days_from_today": 0}
        actual = {**dict.fromkeys(SCENARIO_FIELDS), "asks_profit": False, **self.plan,
                  "option_a_days_from_today": 0, "days_after_option_a": None, "option_b_days_from_today": 7}
        call = SimpleNamespace(type="function_call", name=SCENARIO_TOOL_NAME,
                               arguments=json.dumps(actual, ensure_ascii=False))
        client = SimpleNamespace(responses=SimpleNamespace(
            create=lambda **_: SimpleNamespace(status="completed", output=[call])))
        cases = [{"id": "c", "turns": [{"question": "q", "tool": SCENARIO_TOOL_NAME, "expected": expected}]}]
        report = add_effective_scores(evaluate(cases, client, "test"))
        self.assertEqual((report["exact_matches"], report["effective_exact_matches"]), (0, 1))
        # 오늘 기준 10일을 모델이 A안 기준으로 잘못 넣으면 판매일이 달라 표준화 후에도 틀린다.
        wrong = {**actual, "option_a_days_from_today": 3, "option_b_days_from_today": None, "days_after_option_a": 10}
        call.arguments = json.dumps(wrong, ensure_ascii=False)
        expected = {**self.plan, "option_a_days_from_today": 3, "days_after_option_a": None,
                    "option_b_days_from_today": 10}
        cases[0]["turns"][0]["expected"] = expected
        report = add_effective_scores(evaluate(cases, client, "test"))
        self.assertEqual(report["effective_exact_matches"], 0)


if __name__ == "__main__":
    unittest.main()
