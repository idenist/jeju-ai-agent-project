import csv
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest
from agents.juneok.data_refresh import stage_snapshot, activate_snapshot, resolve_market_root, market_status, snapshot_dir
from agents.juneok.evidence import get_market_evidence
from agents.juneok.config import PROJECT_ROOT


class RefreshTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        report = self.root / "data/reports/auction_quality_report.json"
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({"observed_dates": ["2026-11-03"]}), encoding="utf-8")
        with (PROJECT_ROOT / "data/raw/auction_trades.csv").open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self.headers = reader.fieldnames
            self.row = next(r for r in reader if r["gds_lclsf_cd"] == "06" and r["gds_mclsf_cd"] == "14"
                and r["plor_nm"].startswith("제주특별자치도") and r["trd_se"] == "경매" and r["unit_nm"] == "kg")
        self.today = date(2026, 11, 9)

    def source(self, day):
        source = self.root / (day + ".csv")
        with source.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.headers)
            writer.writeheader()
            writer.writerow({**self.row, "trd_clcln_ymd": day})
        return source

    def test_newer_snapshot_is_selected_and_baseline_unchanged(self):
        baseline = (self.root / "data/reports/auction_quality_report.json").read_bytes()
        manifest = stage_snapshot([self.source("2026-11-04")], "newer", self.root, self.today)
        self.assertEqual(manifest["status"], "VALIDATED")
        activate_snapshot("newer", self.root, self.today)
        self.assertEqual(resolve_market_root(self.root), snapshot_dir(self.root, "newer"))
        self.assertEqual(market_status(self.root, self.today)["observed_last_date"], "2026-11-04")
        self.assertEqual((self.root / "data/reports/auction_quality_report.json").read_bytes(), baseline)

    def test_equal_or_older_snapshot_cannot_claim_refresh(self):
        stage_snapshot([self.source("2026-11-03")], "same", self.root, self.today)
        with self.assertRaises(ValueError):
            activate_snapshot("same", self.root, self.today)
        self.assertFalse((self.root / "artifacts/market-active.json").exists())

    def test_future_or_empty_sources_cannot_be_activated(self):
        with self.assertRaises(ValueError):
            stage_snapshot([self.source("2026-11-10")], "future", self.root, self.today)
        self.assertFalse((snapshot_dir(self.root, "future") / "manifest.json").exists())

    def test_tamper_before_activation_and_after_selection_are_detected(self):
        stage_snapshot([self.source("2026-11-04")], "newer", self.root, self.today)
        path = snapshot_dir(self.root, "newer") / "data/processed/daily_prices.csv"
        original = path.read_bytes()
        path.write_bytes(original + b'\n')
        with self.assertRaises(ValueError):
            activate_snapshot("newer", self.root, self.today)
        path.write_bytes(original)
        activate_snapshot("newer", self.root, self.today)
        path.write_bytes(original + b'\n')
        with self.assertRaises(ValueError):
            resolve_market_root(self.root)
        result = get_market_evidence({"market_code": "110001", "variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": 5}, self.root)
        self.assertEqual(result["status"], "DATA_ERROR")

    def test_invalid_version_paths_are_rejected(self):
        for name in ["../outside", "C:/outside", "x/y", "", "x" * 65]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                snapshot_dir(self.root, name)


if __name__ == "__main__":
    unittest.main()
