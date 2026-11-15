"""Offline tests for season boundary and same-spec price aggregation."""
import csv
import tempfile
import unittest
from pathlib import Path
from agents.common.seasonal_prices import compare_seasonal_prices, REQUIRED


class SeasonalLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.q = {"market_code": "110001", "variety": "조생귤", "grade": "특", "size": "소",
                  "package": "상자", "package_kg": 5, "trade_type": "경매",
                  "month_day": "11-15", "window_days": 7}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, season, *rows):
        p = self.root / 'data' / 'seasons' / season / 'daily_prices.csv'
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('w', encoding='utf-8-sig', newline='') as f:
            fields = sorted(REQUIRED)
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for day, amount, kg, n, warning, *rest in rows:
                record = {"date": day, "market_code": "110001", "variety": "조생귤",
                          "variety_code": "01", "grade": "특", "grade_code": "11",
                          "size": "소", "size_code": "184", "package": "상자", "package_kg": "5.000",
                          "trade_type": "경매", "total_kg": kg, "total_amount_krw": amount,
                          "transaction_count": n, "quality_flag": warning}
                record.update(rest[0] if rest else {})
                writer.writerow(record)

    def test_weighted_price_and_missing_seasons(self):
        self.write("2024-2025", ("2024-11-10", "1000", "1", 1, "OK"),
                   ("2024-11-16", "9000", "3", 4, "LOW_SAMPLE"))
        r = compare_seasonal_prices(self.q, self.root)
        self.assertEqual(r['status'], 'PARTIAL_DATA')
        s = r['seasons'][1]
        self.assertEqual(s['price_per_kg'], '2500.00')
        self.assertEqual(s['observed_days'], 2)
        self.assertEqual(s['transaction_count'], 5)
        self.assertEqual(s['quality_warnings'], ['LOW_SAMPLE'])
        self.assertEqual(r['seasons'][0]['status'], 'NOT_PREPARED')

    def test_other_grade_is_excluded_and_partial_window_marked(self):
        self.write("2026-2027", ("2026-11-10", "3000", "1", 2, "OK"),
                   ("2026-11-11", "99999", "1", 20, "OK", {"grade": "상"}))
        r = compare_seasonal_prices(self.q, self.root)
        self.assertEqual(r['seasons'][3]['price_per_kg'], '3000.00')
        self.assertFalse(r['seasons'][3]['window_complete'])

    def test_no_invented_future_price_or_fallback(self):
        r = compare_seasonal_prices(self.q, self.root)
        self.assertEqual(r['status'], 'NO_DATA')
        self.assertTrue(all(s['price_per_kg'] is None for s in r['seasons']))
        self.assertNotIn('prediction', r)

    def test_invalid_month_and_missing_filters(self):
        self.assertEqual(compare_seasonal_prices({**self.q, 'month_day':'08-12'}, self.root)['status'], 'INVALID_INPUT')
        self.assertEqual(compare_seasonal_prices({**self.q, 'variety':None}, self.root)['status'], 'NEEDS_INPUT')


if __name__ == '__main__':
    unittest.main()
