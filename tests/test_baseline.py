"""감귤 출하 시점 비교의 핵심 계산 및 CSV 조회 단위 테스트."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from agents.baseline import calculate_break_even, describe_history, load_price_observations


class BaselineTests(unittest.TestCase):
    def test_break_even_example(self):
        """제안서의 3톤·7일·2% 예시를 확인한다."""
        result = calculate_break_even(
            current_price_per_kg=Decimal("3000"),
            quantity_kg=Decimal("3000"),
            delay_days=7,
            daily_storage_cost=Decimal("20000"),
            loss_percent=Decimal("2"),
            future_price_per_kg=Decimal("3090"),
        )
        self.assertEqual(result.current_gross, Decimal("9000000"))
        self.assertEqual(result.remaining_kg, Decimal("2940"))
        self.assertEqual(result.extra_storage_cost, Decimal("140000"))
        self.assertEqual(result.difference_if_given, Decimal("-55400"))
        self.assertEqual(result.break_even_price_per_kg.quantize(Decimal("0.01")), Decimal("3108.84"))

    def test_invalid_loss(self):
        """감량률이 100% 이상이면 손익분기를 계산할 수 없다."""
        with self.assertRaises(ValueError):
            calculate_break_even(
                current_price_per_kg=Decimal("1000"),
                quantity_kg=Decimal("100"),
                delay_days=7,
                daily_storage_cost=Decimal("1000"),
                loss_percent=Decimal("100"),
            )

    def test_comparable_csv_only(self):
        """품종·등급·크기가 다른 거래가 가격에 섞이지 않는지 확인한다."""
        fields = [
            "date", "market_code", "market", "variety", "grade", "size", "package",
            "package_kg", "trade_type", "total_kg", "total_amount_krw",
            "transaction_count", "quality_flag",
        ]
        rows = [
            ["2026-10-01", "110001", "서울가락", "조생귤", "특", "소", "상자", "5.000", "경매", "10", "20000", "5", "OK"],
            ["2026-10-01", "110001", "서울가락", "조생귤", "특", "소", "상자", "5.000", "경매", "30", "90000", "6", "OK"],
            ["2026-10-01", "110001", "서울가락", "조생귤", "특", "대", "상자", "5.000", "경매", "100", "1000000", "20", "OK"],
            ["2026-10-08", "110001", "서울가락", "조생귤", "특", "소", "상자", "5.000", "경매", "40", "120000", "10", "OK"],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.csv"
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(fields)
                writer.writerows(rows)
            observed = load_price_observations(
                path, variety="조생귤", grade="특", size="소", package_kg=Decimal("5")
            )
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0].price_per_kg, Decimal("2750"))
        self.assertEqual(observed[0].total_kg, Decimal("40"))
        self.assertIn("9.09%", describe_history(observed, days=7))


if __name__ == "__main__":
    unittest.main()
