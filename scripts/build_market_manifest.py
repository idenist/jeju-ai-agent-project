"""Record source provenance and hashes without changing source datasets."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["data/raw/auction_trades.csv", "data/raw/jeju_shipments.csv",
         "data/processed/daily_prices.csv", "data/processed/daily_variety_prices.csv",
         "data/processed/daily_shipments.csv", "data/processed/monthly_shipments.csv",
         "data/reports/auction_quality_report.json", "data/reports/shipment_quality_report.json",
         "data/reports/shipment_unassigned_rows.csv"]


def build_manifest(root: Path = ROOT) -> dict:
    root = Path(root)
    files = []
    for relative in FILES:
        path = root / relative
        content = path.read_bytes()
        item = {"file": relative, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        if relative.startswith("data/processed/"):
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                item["columns"] = reader.fieldnames
                item["rows"] = sum(1 for _ in reader)
        files.append(item)
    auction = json.loads((root / "data/reports/auction_quality_report.json").read_text(encoding="utf-8-sig"))
    shipment = json.loads((root / "data/reports/shipment_quality_report.json").read_text(encoding="utf-8-sig"))
    try:
        main_sha = subprocess.check_output(["git", "rev-parse", "main"], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        main_sha = None
    return {"schema_version": 1, "source_snapshot_main_sha": main_sha, "files": files,
            "datasets": [
                {"id": "jeju-hub-855", "name": shipment["source"],
                 "url": "https://www.jejudatahub.net/data/view/data/855", "unit": shipment["unit"],
                 "observed_first_date": shipment["source_date_min"], "observed_last_date": shipment["source_date_max"],
                 "last_month_partial": shipment["source_last_month_partial"],
                 "missing_date_rows_excluded": shipment["excluded_rows"],
                 "license_status": "Individual source conditions need confirmation; no CC0 claim.",
                 "preprocessing": "scripts/preprocess_shipments.py"},
                {"id": "auction-source", "name": auction["source"],
                 "url": "https://www.data.go.kr/data/15141810/openapi.do", "unit": "원/kg",
                 "observed_first_date": min(auction["observed_dates"]), "observed_last_date": max(auction["observed_dates"]),
                 "market_scope": "Collected Seoul Garak market observations only; not nationwide current prices.",
                 "license_status": "Record and follow original API conditions; source attribution required.",
                 "preprocessing": "scripts/preprocess_auction.py"}],
            "interpretation": ["Historical data; not a live supply or price feed.",
                               "Shipment and auction category labels are not automatically mapped.",
                               "Non-overlapping source periods do not establish contemporaneous price/supply effects."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    opts = parser.parse_args()
    output = opts.output or opts.root / "data/metadata/juneok_manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_manifest(opts.root), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[완료] 데이터 출처·해시 명세: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
