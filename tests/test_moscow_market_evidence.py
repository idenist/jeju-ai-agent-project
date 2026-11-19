"""Deterministic tests for opt-in auction evidence on a confirmed A/B threshold."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from presenters.market_export import market_markdown, season_observation_rows
from services.future_pair import compare_future_pair_threshold_reference
from services.market_decision import evaluate_market_decision, threshold_observation_eligible
from services.seasonal_prices import SEASONS, season_price_file
from test_integration_future_pair import COMMON, OPTION_B
from test_seasonal_threshold import QUERY, write_season


SELECTION = {key: value for key, value in QUERY.items() if key != "month_day"}


class MoscowMarketEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.root = Path(directory)
        self.pair = compare_future_pair_threshold_reference(
            **{**COMMON, "start_date": "2026-11-05"}, option_b=OPTION_B,
        )

    def evaluate(self, pair=None, selection=SELECTION, *, confirmed=True):
        return evaluate_market_decision(
            self.pair if pair is None else pair, selection, root=self.root,
            allow_threshold_observation=confirmed,
        )

    def test_confirmed_threshold_counts_only_same_spec_clean_days(self):
        write_season(self.root, "2024-2025", [
            ("2024-11-14", 2400, 100, "OK"),
            ("2024-11-15", 2660, 200, "OK"),
            ("2024-11-16", 4000, 100, "LOW_SAMPLE"),
            ("2024-11-16", 5000, 100, "OK"),
            ("2024-11-17", 3000, 100, "OK"),
            ("2024-11-25", 5000, 100, "OK"),
        ])
        before = deepcopy(self.pair)
        self.assertTrue(threshold_observation_eligible(self.pair))

        result = self.evaluate()

        self.assertEqual(result["status"], "OK")
        comparison = result["comparison"]
        self.assertTrue(comparison["threshold_only"])
        self.assertEqual(comparison["option_a_date"], "2026-11-08")
        self.assertEqual(comparison["option_b_date"], "2026-11-15")
        self.assertEqual(comparison["minimum_whole_krw_per_kg"], "2660")
        self.assertIsNone(comparison["difference_vs_a_krw"])
        self.assertEqual(comparison["decision_under_assumptions"], "SCENARIOS_NOT_RANKED")
        self.assertEqual(self.pair, before)
        evidence = result["market_evidence"]
        self.assertEqual(evidence["price_basis"], "gross")
        self.assertEqual(evidence["conditions"], {**QUERY, "package_kg": "5"})
        season = next(item for item in evidence["seasons"] if item["season"] == "2024-2025")
        self.assertEqual((season["days_above"], season["days_clean"], season["excluded_flagged_days"]), (2, 3, 1))
        report = market_markdown(result)
        self.assertIn("2 / 3일", report)
        self.assertIn("2,660원/kg", report)
        self.assertIn("확률", report)

    def test_eligible_threshold_is_not_queried_without_opt_in_and_valid_spec(self):
        with patch("services.market_decision.compare_break_even_to_history") as lookup:
            self.assertEqual(self.evaluate(confirmed=False)["status"], "NOT_APPLICABLE")
            self.assertEqual(self.evaluate(selection=None)["status"], "NEEDS_SPEC")
            self.assertEqual(self.evaluate(selection={**SELECTION, "package_kg": 0})["status"], "NEEDS_SPEC")
            self.assertEqual(self.evaluate(selection={**SELECTION, "trade_type": "정가수의"})["status"],
                             "NOT_COMPARABLE")
            lookup.assert_not_called()

    def test_reference_or_incompatible_result_cannot_be_joined(self):
        uncertain = compare_future_pair_threshold_reference(
            **COMMON, option_b={**OPTION_B, "daily_storage_cost": None},
        )
        no_cost_metadata = {key: value for key, value in self.pair.items() if key != "unknown_cost_fields"}
        with patch("services.market_decision.compare_break_even_to_history") as lookup:
            for pair, status in (
                (uncertain, "NOT_APPLICABLE"),
                (no_cost_metadata, "NOT_APPLICABLE"),
                ({**self.pair, "price_basis": "commission_net"}, "NOT_COMPARABLE"),
                ({**self.pair, "status": "NEEDS_INPUT"}, "NO_PAIR"),
                ({**self.pair, "options_b": [{**self.pair["options_b"][0], "price_per_kg": "2750"}]},
                 "NOT_APPLICABLE"),
            ):
                with self.subTest(status=status, missing=pair.get("unknown_cost_fields")):
                    self.assertFalse(threshold_observation_eligible(pair))
                    self.assertEqual(self.evaluate(pair)["status"], status)
            lookup.assert_not_called()

    def test_all_days_with_quality_warnings_do_not_produce_an_observed_ratio(self):
        for season in SEASONS:
            write_season(self.root, season, [(f"{season[:4]}-11-15", 5000, 100, "LOW_SAMPLE")])

        result = self.evaluate()

        self.assertEqual(result["status"], "NO_CLEAN_DAYS")
        evidence = result["market_evidence"]
        for season in evidence["seasons"]:
            self.assertEqual(season["status"], "NO_CLEAN_DAYS")
            self.assertEqual(season["excluded_flagged_days"], 1)
            self.assertIsNone(season["daily_share_percent"])
            self.assertIsNone(season["kg_share_percent"])
        self.assertTrue(all(count == "-" for _, count, _ in season_observation_rows(evidence)))
        report = market_markdown(result)
        self.assertIn("품질 경고", report)
        self.assertNotIn("0 / 0일", report)

    def test_all_corrupt_seasons_do_not_look_like_a_success_or_zero_observations(self):
        for season in SEASONS:
            path = season_price_file(self.root, season)
            path.parent.mkdir(parents=True)
            path.write_text("invalid_header\nunreadable_price\n", encoding="utf-8")

        result = self.evaluate()

        self.assertEqual(result["status"], "DATA_ERROR")
        evidence = result["market_evidence"]
        self.assertEqual([item["status"] for item in evidence["seasons"]], ["DATA_ERROR"] * len(SEASONS))
        self.assertTrue(all(count == "-" for _, count, _ in season_observation_rows(evidence)))
        self.assertNotIn("0 / 0일", market_markdown(result))
