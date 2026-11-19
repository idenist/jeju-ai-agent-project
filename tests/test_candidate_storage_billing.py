from datetime import date, timedelta
from decimal import Decimal
import json
from unittest import TestCase

from presenters.reporting import render_result, shipment_candidate_amounts
from services.shipment_candidates import compare_shipment_candidates


START = date(2026, 11, 20)
DATES = tuple(START + timedelta(days=days) for days in (3, 8, 10))


class CandidateStorageBillingTests(TestCase):
    def compare(self, mode, **overrides):
        inputs = {
            "excluded_dates": (),
            "start": START,
            "quantity": 3000,
            "basis": "gross",
            "daily_storage": None,
            "other_cost": 5000,
            "fee": 5,
            "by_date": dict(zip(DATES, ((2500, 1), (None, 3), (2800, 5)))),
            "storage_billing": {"mode": mode, "days": 10, "total_krw": Decimal("100000")},
        }
        inputs.update(overrides)
        return compare_shipment_candidates(DATES, **inputs)

    def test_prorated_charge_matches_engine_and_report(self):
        result = self.compare("prorated")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["storage_billing"], {"mode": "prorated", "days": 10, "total_krw": "100000"})
        self.assertEqual(result["daily_storage_cost_krw"], "10000")
        self.assertEqual([Decimal(row["storage_cost_krw"]) for row in result["rows"]], [30000, 80000, 100000])
        self.assertEqual(Decimal(result["reference_proceeds_krw"]), 7018750)
        self.assertEqual(result["rows"][1]["minimum_whole_krw_per_kg"], "2570")
        self.assertIn("보관비 10일 총 100,000원 · 실제 보관일수에 일할 적용", render_result(result))
        self.assertIn("| 80,000원 |", render_result(result))
        self.assertIn("2,570원/kg 이상 필요 · 보관비 80,000원", shipment_candidate_amounts(result))
        self.assertEqual(json.loads(json.dumps(result))["storage_billing"], result["storage_billing"])

    def test_fixed_cost_is_paid_once_and_not_charged_for_same_day(self):
        quotes = dict(zip(DATES, ((2500, 0), (None, 3), (2800, 5))))
        result = self.compare("fixed", start=DATES[0], by_date=quotes)
        self.assertEqual(result["status"], "OK")
        self.assertIsNone(result["daily_storage_cost_krw"])
        self.assertEqual([Decimal(row["storage_cost_krw"]) for row in result["rows"]], [0, 100000, 100000])
        self.assertEqual(Decimal(result["reference_proceeds_krw"]), 7120000)
        self.assertEqual(Decimal(result["rows"][2]["proceeds_krw"]), 7476000)
        self.assertIn("보관비 10일 이내 정액 100,000원", render_result(result))

    def test_nonterminating_proration_and_unconfirmed_loss_preserve_storage_cost(self):
        result = self.compare("prorated",
                              excluded_dates=[DATES[2]],
                              storage_billing={"mode": "prorated", "days": 9, "total_krw": 100000})
        self.assertEqual(result["status"], "OK")
        self.assertEqual(Decimal(result["rows"][1]["storage_cost_krw"]), Decimal(800000) / 9)
        quotes = dict(zip(DATES, ((2500, 1), (None, None), (2800, 5))))
        partial = self.compare("fixed", by_date=quotes, allow_unknown_losses=True)
        self.assertIsNone(partial["rows"][1]["proceeds_krw"])
        self.assertEqual(partial["rows"][1]["storage_cost_krw"], "100000")

    def test_uncovered_days_are_not_extrapolated(self):
        for mode in ("fixed", "prorated"):
            with self.subTest(mode=mode):
                result = self.compare(mode, storage_billing={"mode": mode, "days": 8, "total_krw": 100000})
                self.assertEqual(result["status"], "NEEDS_INPUT")
                self.assertIn("11월 30일 출하까지는 10일", result["message"])
                self.assertNotIn("rows", result)

    def test_daily_and_period_costs_cannot_be_applied_together(self):
        result = self.compare("prorated", daily_storage=10000)
        self.assertEqual(result["status"], "INVALID_INPUT")
        self.assertIn("하나만", result["message"])
