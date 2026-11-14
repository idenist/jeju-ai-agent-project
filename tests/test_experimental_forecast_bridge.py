"""연구용 가격 추정과 기존 A/B 계산기 사이의 최소 회귀 검증 (모델 API·화면 호출 없음)."""
from datetime import date, timedelta
from decimal import Decimal
import csv
import hashlib
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from agents.seunghyuk import forecast_bridge as bridge


class ForecastBridgeTests(unittest.TestCase):
    def test_새_가격표와_가격쌍이_같은_검증묶음이며_손상시_차단(self):
        # [원인/재발 방지] 새 가격표만 사용하고 과거 가격쌍을 공용 데이터에
        # 남겨두면 Agent가 날짜·거래규격이 서로 다른 가격을 함께 설명하게 된다.
        # 따라서 독립 묶음의 파일 해시와 가격쌍 보고서 원천 해시를 함께 검사한다.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            name = "market-20261113"
            folder = root / "artifacts/seunghyuk/datasets" / name
            prices = folder / "data/processed/daily_prices.csv"
            prices.parent.mkdir(parents=True)
            headers = ["date", "quality_flag", *bridge.KEY_FIELDS,
                       "variety", "grade", "size", "avg_price_kg", "total_kg", "transaction_count"]
            values = ["2026-11-13", "OK", *[bridge.EXAMPLE_SPEC[k] for k in bridge.KEY_FIELDS],
                      "조생귤", "특", "소", "2500.00", "100", "5"]
            with prices.open("w", encoding="utf-8-sig", newline="") as out:
                writer = csv.writer(out)
                writer.writerow(headers)
                writer.writerow(values)
            files = ["data/processed/daily_variety_prices.csv",
                     "data/processed/price_comparisons.csv",
                     "data/processed/price_comparison_availability.csv"]
            for relative in files:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("header\n", encoding="utf-8")
            report_root = folder / "data/reports"
            report_root.mkdir(parents=True)
            (report_root / "auction_quality_report.json").write_text(json.dumps({
                "observed_dates": ["2026-11-13"], "daily_prices_rows": 1}), encoding="utf-8")
            price_sha = hashlib.sha256(prices.read_bytes()).hexdigest()
            (report_root / "price_comparison_report.json").write_text(json.dumps({
                "input_sha256": price_sha, "input_rows": 1, "rows": 0, "availability_rows": 0,
            }), encoding="utf-8")
            files += ["data/processed/daily_prices.csv",
                      "data/reports/auction_quality_report.json",
                      "data/reports/price_comparison_report.json"]
            hashes = {relative: hashlib.sha256((folder / relative).read_bytes()).hexdigest() for relative in files}
            (folder / "manifest.json").write_text(json.dumps({
                "status": "OFFLINE_VALIDATED", "agent_activated": False, "snapshot_name": name,
                "observed_date_min": "2026-11-13", "observed_date_max": "2026-11-13",
                "csv_rows": {"daily_prices.csv": 1, "price_comparisons.csv": 0,
                             "price_comparison_availability.csv": 0}, "sha256": hashes,
            }), encoding="utf-8")
            with patch.dict("os.environ", {bridge.EXPERIMENT_DATASET_ENV: name}):
                selected = bridge.selected_experiment_source(root)
                self.assertEqual(selected["dataset_id"], name)
                self.assertEqual(selected["price_file"], prices)
                self.assertEqual(bridge._read_source_prices(prices)[0]["date"], date(2026, 11, 13))
                # 가격표만 바꿔도, 가격쌍만 바꿔도 공용 자료로 조용히 대체하지 않는다.
                pairs = folder / "data/processed/price_comparisons.csv"
                pairs.write_text("tampered\n", encoding="utf-8")
                with self.assertRaises(bridge.ForecastUnavailable) as ctx:
                    bridge.selected_experiment_source(root)
                self.assertEqual(ctx.exception.code, "EXPERIMENT_DATASET_INVALID")

    def test_연구용_데이터이름_경로탈출_차단(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict("os.environ", {bridge.EXPERIMENT_DATASET_ENV: "../data"}):
                with self.assertRaises(bridge.ForecastUnavailable) as ctx:
                    bridge.selected_experiment_source(Path(temp))
                self.assertEqual(ctx.exception.code, "INVALID_DATASET_NAME")

    def test_과거_정상거래만_사용해_모델특징을_생성(self):
        d = date(2026, 11, 3)
        key = bridge._spec_key(bridge.EXAMPLE_SPEC)
        def row(offset, price, kg=100):
            return {"date": d + timedelta(days=offset), "key": key,
                    "price": Decimal(str(price)), "kg": Decimal(str(kg)), "count": Decimal(10)}
        # 7일 창은 -6일부터 기준일까지이며, 미래 +1일은 절대 사용하지 않는다.
        rows = [row(-7, 500), row(-3, 1000), row(0, 1200), row(1, 100000)]
        values, evidence = bridge._features(rows, spec=bridge.EXAMPLE_SPEC, as_of=d)
        self.assertEqual(values[0], 1200)
        self.assertEqual(values[6], 1000)
        self.assertEqual(values[7], 500)
        self.assertEqual(values[8], 1100)
        self.assertEqual(values[9], 200)
        self.assertEqual(values[10], 2)
        self.assertEqual(values[5], 33)  # 10월 1일 = 0, 11월 3일 = 33
        self.assertEqual(len(values), len(bridge.NUMERIC_FIELDS) + len(bridge.KEY_FIELDS))
        self.assertEqual(evidence["source_observed_days_in_last_7"], 2)

    def test_비지원_10일_예측은_모델을_열기_전에_차단(self):
        with self.assertRaises(bridge.ForecastUnavailable) as ctx:
            bridge.predict_prices(as_of=date(2026, 11, 3), spec=bridge.EXAMPLE_SPEC,
                                  horizons=(3, 10), verify_baseline=False)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_HORIZON")

    def test_예측가격을_기존_A_B_엔진에_연결하고_출처를_수정(self):
        fixture = {"status": "EXPERIMENTAL_ONLY", "warnings": ["실험용 결과"],
                   "forecasts": {"3": {"price_krw_per_kg": "2500.00"},
                                 "14": {"price_krw_per_kg": "2750.00"}}}
        with patch.object(bridge, "predict_prices", return_value=fixture) as predictor:
            result = bridge.compare_experimental(
                as_of=date(2026, 11, 3), spec=bridge.EXAMPLE_SPEC,
                quantity=3000, price_basis="gross",
                option_a=bridge.EXAMPLE_A, option_b=bridge.EXAMPLE_B,
            )
        self.assertEqual(result["status"], "EXPERIMENTAL_ONLY", result)
        self.assertFalse(result["deployment_approved"])
        self.assertEqual(predictor.call_args.kwargs["horizons"], (3, 14))
        calculated = result["calculation"]
        self.assertEqual(calculated["status"], "OK")
        self.assertEqual(Decimal(calculated["option_a"]["proceeds_krw"]), Decimal("7018750"))
        self.assertEqual(Decimal(calculated["options_b"][0]["difference_vs_a_krw"]), Decimal("201850"))
        self.assertIn("연구용 ML 추정", calculated["evidence"]["option_a"]["summary"])
        self.assertIn("연구용 ML 추정", calculated["evidence"]["manual_b"]["summary"])
        self.assertEqual(calculated["evidence"]["option_a"]["price_type"], "experimental_model_estimate")
        self.assertEqual(calculated["evidence"]["manual_b"]["price_source"], "연구용 ML 추정")
        self.assertIn("실험용 결과", calculated["warnings"])


if __name__ == "__main__":
    unittest.main()
