import json
import unittest
from types import SimpleNamespace

from scripts.evaluate_price_extraction import PROPERTIES, TOOL_NAME, evaluate, score


def full(**fields):
    row = dict.fromkeys(PROPERTIES)
    row.update(fields)
    return row


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


if __name__ == "__main__":
    unittest.main()
