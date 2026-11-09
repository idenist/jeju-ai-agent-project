"""실행: python -m unittest discover -s tests -v (추가 설치 불필요)."""
import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_price_comparisons import (  # noqa: E402
    FIELDS, REQUIRED, KEY_FIELDS, build_comparisons, load_prices, run,
)


def row(day="2026-10-01", price="100.00", **changes):
    from decimal import Decimal
    result = {
        "date": day, "market_code": "110001", "market": "서울가락",
        "variety_code": "01", "variety": "조생귤", "grade_code": "11", "grade": "특",
        "size_code": "184", "size": "소", "package": "상자", "package_kg": "5.000",
        "trade_type": "경매", "avg_price_kg": price, "total_kg": "10.000",
        "total_amount_krw": str(Decimal(price) * 10),
        "transaction_count": "10", "quality_flag": "OK",
    }
    result.update(changes)
    return result


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input = self.root / "daily_prices.csv"

    def write(self, rows, fields=REQUIRED):
        with self.input.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return load_prices(self.input)[0]

    def test_calendar_intervals_and_change_formula(self):
        prices = self.write([row(), row("2026-10-04", "110"), row("2026-10-08", "90"), row("2026-10-15", "100")])
        attempts, _ = build_comparisons(prices)
        base = {r["interval_days"]: r for r in attempts if r["base_date"] == "2026-10-01"}
        self.assertEqual(base[3]["price_change_pct"], "10.000000")
        self.assertEqual(base[7]["price_change_pct"], "-10.000000")
        self.assertEqual(base[14]["price_change_pct"], "0.000000")
        self.assertEqual(base[7]["price_change_krw_kg"], "-10.00")

    def test_does_not_choose_nearest_available_date(self):
        prices = self.write([row(), row("2026-10-05")])
        attempts, _ = build_comparisons(prices, [3])
        self.assertEqual(attempts[0]["target_date"], "2026-10-04")
        self.assertEqual(attempts[0]["comparison_status"], "UNAVAILABLE")
        self.assertEqual(attempts[0]["unavailable_reason"], "NO_MARKET_OBSERVATION_ON_TARGET_DATE")
        self.assertEqual(attempts[0]["price_change_pct"], "")
        self.assertEqual(attempts[0]["target_price_kg"], "")

    def test_last_date_is_outside_observation_range(self):
        prices = self.write([row()])
        attempts, _ = build_comparisons(prices, [7])
        self.assertEqual(attempts[0]["unavailable_reason"], "OUTSIDE_OBSERVED_RANGE")

    def test_every_identity_field_prevents_incorrect_join(self):
        alternatives = {
            "market_code": "120001", "variety_code": "06", "grade_code": "12",
            "size_code": "183", "package": "봉지", "package_kg": "10", "trade_type": "정가수의",
        }
        for field, value in alternatives.items():
            with self.subTest(field=field):
                prices = self.write([row(), row("2026-10-08", **{field: value})])
                attempts, _ = build_comparisons(prices, [7])
                self.assertFalse(any(r["comparison_status"] != "UNAVAILABLE" for r in attempts))

    def test_date_exists_but_condition_does_not(self):
        prices = self.write([row(), row("2026-10-08", size_code="183", size="중")])
        attempts, _ = build_comparisons(prices, [7])
        self.assertEqual(attempts[0]["unavailable_reason"], "NO_MATCHING_CONDITION")

    def test_numeric_package_format_is_equivalent(self):
        prices = self.write([row(package_kg="5"), row("2026-10-08", package_kg="5.000")])
        attempts, _ = build_comparisons(prices, [7])
        self.assertEqual(attempts[0]["comparison_status"], "COMPARABLE")

    def test_duplicate_normalized_keys_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "중복"):
            self.write([row(package_kg="5"), row(package_kg="5.000")])

    def test_missing_grade_and_low_sample_not_silently_accepted(self):
        prices = self.write([row(grade="미상", grade_code=".", transaction_count="2"),
                             row("2026-10-08", grade="미상", grade_code=".")])
        attempts, report = build_comparisons(prices, [7])
        pair = attempts[0]
        self.assertEqual(pair["comparison_status"], "COMPARABLE_WITH_WARNINGS")
        self.assertIn("GRADE_UNKNOWN", pair["comparison_warnings"])
        self.assertIn("LOW_SAMPLE", pair["comparison_warnings"])
        self.assertEqual(report["by_interval"]["7"]["both_source_ok_pairs"], 1)
        self.assertEqual(report["by_interval"]["7"]["comparable_without_warnings"], 0)

    def test_source_warning_is_preserved(self):
        prices = self.write([row(quality_flag="CUSTOM_WARNING"), row("2026-10-08")])
        attempts, _ = build_comparisons(prices, [7])
        self.assertIn("CUSTOM_WARNING", attempts[0]["comparison_warnings"])
        self.assertEqual(attempts[0]["both_source_ok"], "false")

    def test_label_mismatch_is_not_eligible(self):
        prices = self.write([row(), row("2026-10-08", grade="상")])
        attempts, _ = build_comparisons(prices, [7])
        self.assertIn("LABEL_MISMATCH", attempts[0]["comparison_warnings"])
        self.assertEqual(attempts[0]["comparison_status"], "COMPARABLE_WITH_WARNINGS")

    def test_invalid_numbers_dates_and_fractional_counts_fail(self):
        cases = [dict(avg_price_kg="NaN"), dict(avg_price_kg="0"), dict(total_kg="-1"),
                 dict(total_amount_krw="Infinity"), dict(package_kg="0"),
                 dict(transaction_count="1.5"), dict(date="2026-02-30"), dict(date="20261001")]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.write([row(**changes)])

    def test_inconsistent_price_amount_weight_fails(self):
        with self.assertRaisesRegex(ValueError, "금액/중량"):
            self.write([row(total_amount_krw="1")])

    def test_empty_input_and_missing_columns_fail(self):
        with self.assertRaisesRegex(ValueError, "관측 행"):
            self.write([])
        with self.assertRaisesRegex(ValueError, "필수 컬럼"):
            self.write([row()], fields=[f for f in REQUIRED if f != "grade"])

    def test_interval_validation(self):
        prices = self.write([row()])
        for intervals in [[], [0], [-1], [367], [True]]:
            with self.subTest(intervals=intervals), self.assertRaises(ValueError):
                build_comparisons(prices, intervals)
        attempts, report = build_comparisons(prices, [7, 3, 7])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(report["interval_days"], [3, 7])

    def test_outputs_trace_source_preserve_input_and_are_reproducible(self):
        self.write([row(package='상자,"특수"'), row("2026-10-08", package='상자,"특수"')])
        original = self.input.read_bytes()
        out, reports = self.root / "processed", self.root / "reports"
        report = run(self.input, out, reports)
        self.assertEqual(self.input.read_bytes(), original)
        self.assertEqual(report["input_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(report["rows"], 1)
        self.assertEqual(report["availability_rows"], 6)
        with (out / "price_comparisons.csv").open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(reader.fieldnames, list(FIELDS))
        self.assertEqual(rows[0]["package"], '상자,"특수"')
        self.assertIn("7일", (reports / "price_comparison_coverage.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((reports / "price_comparison_report.json").read_text(encoding="utf-8")), report)
        before = {p.name: p.read_bytes() for d in [out, reports] for p in d.iterdir()}
        run(self.input, out, reports)
        self.assertEqual(before, {p.name: p.read_bytes() for d in [out, reports] for p in d.iterdir()})

    def test_output_cannot_overwrite_input(self):
        self.input = self.root / "price_comparisons.csv"
        self.write([row()])
        original = self.input.read_bytes()
        with self.assertRaisesRegex(ValueError, "겹칩니다"):
            run(self.input, self.root, self.root / "reports")
        self.assertEqual(original, self.input.read_bytes())

    def test_current_dataset_counts_against_independent_join(self):
        source = ROOT / "data/processed/daily_prices.csv"
        if not source.exists():
            self.skipTest("통합 검증용 가격 파일 없음")
        from datetime import date, timedelta
        from decimal import Decimal
        with source.open(encoding="utf-8-sig", newline="") as f:
            raw = list(csv.DictReader(f))
        def key(r):
            return tuple(Decimal(r[k]) if k == "package_kg" else r[k] for k in KEY_FIELDS)
        index = {(r["date"], key(r)): r for r in raw}
        prices, _ = load_prices(source)
        attempts, report = build_comparisons(prices)
        for n in [3, 7, 14]:
            paired = []
            for r in raw:
                later = (date.fromisoformat(r["date"]) + timedelta(days=n)).isoformat()
                other = index.get((later, key(r)))
                if other:
                    paired.append((r, other))
            self.assertEqual(report["by_interval"][str(n)]["matched_pairs"], len(paired))
            self.assertEqual(report["by_interval"][str(n)]["both_source_ok_pairs"],
                             sum(a["quality_flag"] == b["quality_flag"] == "OK" for a, b in paired))
        self.assertEqual(len(attempts), len(raw) * 3)


if __name__ == "__main__":
    unittest.main()
