"""CSV preview/apply checks: independent fixtures, no live APIs or shared writes."""
import csv
import io
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest

from agents.juneok.config import PROJECT_ROOT
from agents.juneok.csv_import import preview_auction_csv, apply_auction_preview
from agents.juneok.data_refresh import market_status, snapshot_dir
from agents.juneok.evidence import get_market_evidence
from scripts.preprocess_auction import run


class CsvImportTests(unittest.TestCase):
    def setUp(self):
        tmp = PROJECT_ROOT / "artifacts/tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=tmp)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        with (PROJECT_ROOT / "data/raw/auction_trades.csv").open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self.headers = reader.fieldnames
            self.row = next(r for r in reader if r["gds_lclsf_cd"] == "06" and r["gds_mclsf_cd"] == "14"
                            and r["plor_nm"].startswith("제주특별자치도") and r["trd_se"] == "경매"
                            and r["unit_nm"] == "kg")
        self.row["trd_clcln_ymd"] = "2026-11-03"
        source = self.root / "data/raw/auction_trades.csv"
        source.parent.mkdir(parents=True)
        source.write_bytes(self.payload([self.row]))
        run([source], self.root / "data/processed", self.root / "data/reports")
        self.today = date(2026, 11, 9)
        self.new = {**self.row, "trd_clcln_ymd": "2026-11-04"}
        self.original = source.read_bytes()

    def payload(self, rows, headers=None, encoding="utf-8-sig"):
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=headers or self.headers)
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode(encoding)

    def preview(self, payload=None):
        return preview_auction_csv(payload or self.payload([self.new]), self.root, self.today)

    def test_preview_is_not_apply_and_new_source_reaches_evidence(self):
        preview = self.preview(self.payload([self.row, self.new]))
        self.assertEqual(market_status(self.root, self.today)["observed_last_date"], "2026-11-03")
        self.assertEqual(preview["combined_accepted_rows"], 2)  # overlapping trade counted once
        apply_auction_preview(preview, self.root, self.today)
        args = {"market_code": self.row["whsl_mrkt_cd"], "variety": "조생귤", "grade": "미상",
                "size": "미상", "package": self.row["pkg_nm"], "package_kg": float(self.row["unit_qty"])}
        # The sample may use a different citrus variety; query its actual canonical name.
        from scripts.preprocess_auction import canonical_variety
        args["variety"] = canonical_variety(self.row["gds_sclsf_cd"], self.row["gds_sclsf_nm"])
        result = get_market_evidence(args, self.root)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["source"]["snapshot_id"], preview["snapshot_id"])
        self.assertEqual(result["source"]["source_last_date"], "2026-11-04")
        self.assertEqual((self.root / "data/raw/auction_trades.csv").read_bytes(), self.original)

    def test_cp949_and_reordered_columns_keep_overlap_deduplication(self):
        preview = self.preview(self.payload([self.row, self.new], list(reversed(self.headers)), "cp949"))
        self.assertEqual(preview["encoding"], "cp949")
        self.assertEqual(preview["combined_accepted_rows"], 2)

    def test_invalid_new_rows_cannot_hide_behind_valid_baseline(self):
        for row in [self.row, {**self.new, "unit_nm": "알수없음"},
                    {**self.new, "totprc": "0"}, {**self.new, "trd_clcln_ymd": "2026-11-10"}]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.preview(self.payload([row]))
        self.assertFalse((self.root / "artifacts/market-active.json").exists())

    def test_bad_encoding_headers_and_row_width_are_rejected(self):
        valid = self.payload([self.new]).decode("utf-8-sig")
        samples = [b"\xff\xff", b"", b"\0abc", b"date,price\n2026-11-04,3000\n",
                   (valid.splitlines()[0] + "," + self.headers[0] + "\n").encode(),
                   (valid + "extra,short,row\n").encode()]
        for payload in samples:
            with self.subTest(payload=payload[:25]), self.assertRaises(ValueError):
                preview_auction_csv(payload, self.root, self.today)

    def test_preview_explains_excluded_rows_before_apply(self):
        preview = self.preview(self.payload([self.new, {**self.new, "totprc": "0"}]))
        self.assertEqual(preview["input_rows"], 2)
        self.assertEqual(preview["accepted_rows"], 1)
        self.assertEqual(preview["rejected_rows"], 1)
        self.assertTrue(preview["rejected_by_primary_reason"])

    def test_changed_raw_or_processed_or_manifest_cannot_apply(self):
        for relative in ["data/raw/auction_1.csv", "data/processed/daily_prices.csv", "manifest.json"]:
            with self.subTest(relative=relative):
                preview = self.preview()
                path = snapshot_dir(self.root, preview["snapshot_id"]) / relative
                path.write_bytes(path.read_bytes() + b"\n")
                with self.assertRaises(ValueError):
                    apply_auction_preview(preview, self.root, self.today)
                self.assertFalse((self.root / "artifacts/market-active.json").exists())

    def test_changed_selection_requires_another_preview(self):
        older = self.preview()
        newer = self.preview(self.payload([{**self.new, "trd_clcln_ymd": "2026-11-05"}]))
        apply_auction_preview(newer, self.root, self.today)
        with self.assertRaises(ValueError):
            apply_auction_preview(older, self.root, self.today)
        self.assertEqual(market_status(self.root, self.today)["snapshot_id"], newer["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
