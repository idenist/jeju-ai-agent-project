import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agents.jeonghyeon.price_history import DEFAULT_FILE
from agents.jeonghyeon.price_scenario import PRICES_FILE, REPORT_FILE, content_sha256, execute_scenario_tool

SPEC = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": 5,
        "option_a_sale_date": "2026-11-13", "option_b_sale_date": "2026-11-20",
        "option_a_price_per_kg": 2500}


def run(**changes):
    args = {**SPEC, **changes}
    return execute_scenario_tool({k: v for k, v in args.items() if v is not None})


def scenario(result, basis):
    return next(s for s in result["scenarios"] if s["basis"] == basis)


class PriceScenarioTest(unittest.TestCase):
    def test_seven_day_gap_gives_assumed_b_prices(self):
        result = run()
        self.assertEqual((result["status"], result["interval_days"], result["clean_pairs"]), ("OK", 7, 20))
        median = scenario(result, "median")
        self.assertEqual((median["change_pct"], median["option_b_price_per_kg"]), ("-3.63", "2409.37"))
        self.assertEqual((scenario(result, "min")["change_pct"], scenario(result, "max")["change_pct"]),
                         ("-33.54", "89.30"))
        self.assertTrue(all(s["price_type"] == "assumption" for s in result["scenarios"]))
        self.assertFalse(result["is_future_forecast"])

    def test_fourteen_day_gap_keeps_spec(self):
        result = run(option_b_sale_date="2026-11-27")
        self.assertEqual((result["interval_days"], result["clean_pairs"]), (14, 15))
        self.assertEqual(result["conditions"]["grade"], "특")

    def test_missing_a_price_still_shows_history(self):
        result = run(option_a_price_per_kg=None)
        self.assertEqual(result["status"], "NEEDS_CLARIFICATION")
        self.assertEqual(result["missing_fields"], ["option_a_price_per_kg"])
        self.assertEqual(result["clean_pairs"], 20)
        self.assertIsNone(scenario(result, "median")["option_b_price_per_kg"])

    def test_unsupported_gap_allows_manual_price(self):
        result = run(option_b_sale_date="2026-11-23")
        self.assertEqual(result["status"], "INTERVAL_NOT_AVAILABLE")
        self.assertEqual(result["available_intervals"], [3, 7, 14])
        self.assertTrue(result["manual_price_allowed"])

    def test_no_record_allows_manual_price(self):
        result = run(variety="온주", package_kg=10)
        self.assertEqual(result["status"], "NO_DATA")
        self.assertTrue(result["manual_price_allowed"])
        self.assertNotIn("scenarios", result)

    def test_b_before_a_is_input_error(self):
        result = run(option_b_sale_date="2026-11-10")
        self.assertEqual((result["status"], result["error_kind"]), ("ERROR", "INVALID_INPUT"))

    def test_missing_dates_are_asked(self):
        result = run(option_a_sale_date=None)
        self.assertEqual(result["missing_fields"], ["option_a_sale_date"])

    def test_b_price_uses_all_clean_pairs_not_examples(self):
        result = run()
        self.assertLess(len(result["examples"]), result["clean_pairs"])
        expected = Decimal("2500") * (1 + Decimal(scenario(result, "max")["change_pct"]) / 100)
        self.assertAlmostEqual(float(scenario(result, "max")["option_b_price_per_kg"]), float(expected), places=0)


class SourceVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def copy_prices(self, name, transform):
        target = self.tmp / name
        target.write_bytes(transform(PRICES_FILE.read_bytes()))
        return target

    def test_line_ending_change_is_same_version(self):
        crlf = self.copy_prices("crlf.csv", lambda b: b.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        lf = self.copy_prices("lf.csv", lambda b: b.replace(b"\r\n", b"\n"))
        for path in (crlf, lf):
            self.assertEqual(execute_scenario_tool({**SPEC}, prices_file=path, report_file=REPORT_FILE)["status"], "OK")

    def test_changed_prices_are_rejected(self):
        changed = self.copy_prices("changed.csv", lambda b: b + b"\n")
        result = execute_scenario_tool({**SPEC}, prices_file=changed, report_file=REPORT_FILE)
        self.assertEqual((result["status"], result["error_kind"]), ("ERROR", "DATA_ERROR"))
        self.assertIn("다른 버전", result["error"])


    def copy_table(self, name, transform):
        target = self.tmp / name
        target.write_bytes(transform(DEFAULT_FILE.read_bytes()))
        return target

    def test_changed_table_is_rejected_even_if_prices_match(self):
        # 가격 원본과 보고서는 그대로 두고 비교표만 바꾼 경우
        lines = DEFAULT_FILE.read_bytes().replace(b"\r\n", b"\n").split(b"\n")
        older = self.copy_table("price_comparison_availability.csv", lambda _: b"\n".join(lines[:-2] + [b""]))
        result = execute_scenario_tool({**SPEC}, data_file=older)
        self.assertEqual((result["status"], result["error_kind"]), ("ERROR", "DATA_ERROR"))
        self.assertIn("비교표 파일", result["error"])

    def test_table_line_endings_do_not_matter(self):
        crlf = self.copy_table("crlf.csv", lambda b: b.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertEqual(execute_scenario_tool({**SPEC}, data_file=crlf)["status"], "OK")


class HashRuleTest(unittest.TestCase):
    def test_build_and_check_use_same_rule(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "scripts/build_price_comparisons.py"
        spec = importlib.util.spec_from_file_location("build_price_comparisons", path)
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        sample = b"a,b\r\n1,2\r\n"
        self.assertEqual(build.content_sha256(sample), content_sha256(sample))
        self.assertEqual(content_sha256(sample), content_sha256(sample.replace(b"\r\n", b"\n")))


if __name__ == "__main__":
    unittest.main()
