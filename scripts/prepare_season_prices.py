"""Prepare ONLY missing seasonal daily price tables from existing data/seasons/*/raw CSVs.

Run: python -m scripts.prepare_season_prices
Never touches data/processed or existing season tables. Keeps source CSV untouched.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from scripts.preprocess_auction import run as preprocess

ROOT = Path(__file__).resolve().parents[1]
SEASONS = ("2023-2024", "2024-2025", "2025-2026", "2026-2027")


def prepare(root: Path = ROOT, seasons=SEASONS) -> dict:
    statuses = {}
    for season in seasons:
        base = Path(root) / "data" / "seasons" / season
        target = base / "daily_prices.csv"
        if target.is_file():
            print(f"[{season}] 기존 가공본 유지", flush=True)
            statuses[season] = "EXISTS"
            continue
        sources = sorted((base / "raw").glob("*.csv"))
        if not sources:
            print(f"[{season}] 원본 CSV가 없어 건너뜀", flush=True)
            statuses[season] = "MISSING_RAW"
            continue
        # Stage outside season folder, then install only absent outputs.
        with tempfile.TemporaryDirectory(prefix=f"season-{season}-") as folder:
            temp = Path(folder)
            print(f"[{season}] 원본 {len(sources)}개 전처리", flush=True)
            report = preprocess(sources, temp / "processed", temp / "reports")
            if report["accepted_rows"] <= 0:
                raise ValueError(f"{season}: 분석 가능한 경매 데이터가 없습니다.")
            files = ((temp / "processed" / "daily_prices.csv", base / "daily_prices.csv"),
                     (temp / "processed" / "daily_variety_prices.csv", base / "daily_variety_prices.csv"),
                     (temp / "reports" / "auction_quality_report.json", base / "보고서" / "auction_quality_report.json"))
            if any(dest.exists() for src, dest in files):
                raise ValueError(f"{season}: 일부 결과가 이미 존재합니다. 수동 병합 없이 중단합니다.")
            for src, dest in files:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            statuses[season] = "CREATED"
            print(f"[{season}] 완료: 원본 {report['input_rows']:,}건 → 거래 {report['accepted_rows']:,}건", flush=True)
    return statuses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", choices=SEASONS, action="append")
    args = parser.parse_args()
    print(json.dumps(prepare(seasons=args.season or SEASONS), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
