import unittest

from agents.jeonghyeon.agent import format_result
from agents.jeonghyeon.price_history import get_price_history, normalize_conditions


SPEC = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
        "package_kg": 5, "interval_days": 7}


class LabelNormalizationTests(unittest.TestCase):
    def test_presentation_questions_return_comparable_pairs(self):
        for interval, count in ((7, 20), (14, 15)):
            with self.subTest(interval=interval):
                result = get_price_history({**SPEC, "grade": "특등급", "size": "소과",
                                            "interval_days": interval})
                self.assertEqual(result["status"], "OK")
                self.assertEqual(result["clean_pairs"], count)
                self.assertIn("등급 특등급 → 특", format_result(result))

    def test_box_alias_keeps_separate_weight(self):
        result = get_price_history({**SPEC, "package": "5kg 박스"})
        self.assertEqual(result["clean_pairs"], 20)
        self.assertEqual(result["conditions"]["package"], "상자")
        self.assertEqual(result["normalized_conditions"]["package"]["to"], "상자")

    def test_weight_only_package_prompts_for_package(self):
        result = get_price_history({**SPEC, "package": "5키로"})
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION")
        self.assertEqual(result["missing_fields"], ["package"])

    def test_existing_labels_are_unchanged(self):
        self.assertEqual(get_price_history(SPEC)["normalized_conditions"], {})

    def test_unknown_label_is_not_guessed(self):
        self.assertEqual(get_price_history({**SPEC, "grade": "특품"})["status"], "NO_DATA")

    def test_normalization_requires_target_label_in_data(self):
        args = {"grade": "특등급", "size": "소과", "package": "박스"}
        effective, changed = normalize_conditions(args, [{"grade": "등외", "size": "대", "package": "봉지"}])
        self.assertEqual(effective, args)
        self.assertEqual(changed, {})
