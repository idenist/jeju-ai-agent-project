import unittest

from agents.jeonghyeon.price_history import execute_tool

SPEC = {"variety": "조생귤", "grade": "특", "size": "소", "interval_days": 7}


def run(package, package_kg):
    return execute_tool("get_price_history", {**SPEC, "package": package, "package_kg": package_kg})


class PackageWeightTest(unittest.TestCase):
    def test_conflicting_weights_are_not_guessed(self):
        result = run("10kg 박스", 5)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_kind"], "INVALID_INPUT")
        self.assertIn("10kg과 포장 중량 5kg", result["error"])

    def test_weight_after_package_name_is_split(self):
        result = run("상자 5kg", 5)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["clean_pairs"], 20)
        self.assertEqual(result["normalized_conditions"]["package"], {"from": "상자 5kg", "to": "상자"})

    def test_matching_weight_with_box_alias(self):
        self.assertEqual(run("5kg 박스", 5)["clean_pairs"], 20)

    def test_weight_only_package_is_asked_again(self):
        result = run("5키로", 5)
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION")
        self.assertEqual(result["missing_fields"], ["package"])

    def test_weight_in_package_does_not_fill_missing_weight(self):
        # 포장명의 중량으로 package_kg를 채우지 않고 기존처럼 다시 묻는다.
        result = run("10kg 박스", None)
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION")
        self.assertEqual(result["missing_fields"], ["package_kg"])

    def test_two_weights_in_package_conflict(self):
        self.assertEqual(run("5kg 10kg 박스", None)["error_kind"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
