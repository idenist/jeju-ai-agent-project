"""Small deterministic seasonal threshold tests; no model calls or dataset modifications."""
import csv
import tempfile
import unittest
from pathlib import Path

from agents.common.seasonal_threshold import compare_break_even_to_history

QUERY = {
    "market_code": "110001",
    "variety": "조생귤",
    "grade": "특",
    "size": "소",
    "package": "상자",
    "package_kg": 5,
    "trade_type": "경매",
    "month_day": "11-15",
    "window_days": 7,
}


def write_season(root, name, specs):
    folder = root / "data/seasons" / name
    folder.mkdir(parents=True)
    columns = [
        "date", "market_code", "market", "variety_code", "variety", "grade_code",
        "grade", "size_code", "size", "package", "package_kg", "trade_type",
        "avg_price_kg", "total_kg", "total_amount_krw", "transaction_count",
        "quality_flag",
    ]
    with (folder / "daily_prices.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for day, price, mass, flag in specs:
            w.writerow(dict(zip(columns, [
                day, "110001", "서울가락", "01", "조생귤", "11", "특", "184", "소",
                "상자", "5", "경매", str(price), str(mass), str(price * mass), "10", flag,
            ])))


class SeasonalThresholdTests(unittest.TestCase):
    def test_daily_threshold_not_transaction_probability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_season(root, "2024-2025", [
                ("2024-11-14", 2400, 100, "OK"),
                ("2024-11-15", 3000, 200, "OK"),
                ("2024-11-16", 4000, 100, "LOW_SAMPLE"),
            ])
            result = compare_break_even_to_history(QUERY, "2660", price_basis="gross", root=root)
            self.assertEqual(result["status"], "OK")
            a = next(x for x in result["seasons"] if x["season"] == "2024-2025")
            self.assertEqual(a["days_clean"], 2)
            self.assertEqual(a["days_above"], 1)
            self.assertEqual(a["daily_share_percent"], "50.0")
            self.assertEqual(a["kg_share_percent"], "66.7")
            self.assertEqual(a["excluded_flagged_days"], 1)
            self.assertFalse(a["window_complete"])
            missing = next(x for x in result["seasons"] if x["season"] == "2023-2024")
            self.assertEqual(missing["status"], "NOT_PREPARED")

    def test_net_price_must_not_be_compared_to_auction_gross(self):
        result = compare_break_even_to_history(QUERY, "2660", price_basis="commission_net")
        self.assertEqual(result["status"], "NOT_COMPARABLE")

    def test_no_matching_trade_is_not_mistaken_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_season(root, "2023-2024", [("2023-11-15", 2500, 100, "OK")])
            query = {**QUERY, "trade_type": "정가수의"}
            result = compare_break_even_to_history(query, "2660", price_basis="gross", root=root)
            self.assertEqual(result["status"], "NO_DATA")
            self.assertEqual(result["seasons"][0]["status"], "NO_DATA")

    def test_invalid_threshold_does_not_crash(self):
        result = compare_break_even_to_history(QUERY, "NaN", price_basis="gross")
        self.assertEqual(result["status"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
