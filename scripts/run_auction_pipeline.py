"""시즌 경매 원본을 독립 재생성하고 기존 결과와 비교하는 단일 실행 진입점.

기존 전처리·가격쌍 생성 함수를 그대로 사용하며, data/ 운영 파일은 수정하지 않는다.
기본 실행 시 임시 폴더의 결과는 검증 직후 삭제한다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
from itertools import zip_longest
from pathlib import Path

from scripts.build_price_comparisons import run as build_price_comparisons
from scripts.preprocess_auction import run as preprocess_auction

ROOT = Path(__file__).resolve().parents[1]
SEASONS = ("2024-2025", "2025-2026")
OUTPUTS = (
    "daily_prices.csv",
    "daily_variety_prices.csv",
    "price_comparisons.csv",
    "price_comparison_availability.csv",
)


def sha256(path: Path) -> str:
    """큰 원본도 메모리에 전부 올리지 않고 해시를 계산한다."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def sources_for(season: str) -> tuple[list[Path], dict]:
    """기존 시즌 검증 명세에서 원본 경로·기대 해시를 읽는다."""
    manifest_path = ROOT / "data" / "seasons" / season / "보고서" / "시즌통합_검증.json"
    report = json.loads(manifest_path.read_text(encoding="utf-8"))
    periods = report.get("source_periods")
    if not periods or report.get("status") != "OK":
        raise ValueError(f"{season}: 원본 검증 명세가 불완전합니다.")
    paths = []
    for item in periods:
        # 원본 보고서는 Windows 경로 구분자를 사용한다. 다른 OS에서도 읽게 정규화한다.
        path = Path(item["source_file"].replace("\\", "/"))
        if not path.is_absolute():
            path = ROOT / path
        if not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"{season}: 프로젝트 외부 원본 경로를 거부합니다.")
        if not path.is_file() or sha256(path) != item["source_sha256"]:
            raise ValueError(f"{season}: 원본 파일 누락 또는 해시 불일치: {path.name}")
        paths.append(path)
    return paths, report


def compare_csv(expected: Path, actual: Path) -> int:
    """BOM·줄바꿈과 무관하게 컬럼 순서·모든 행의 내용을 비교한다."""
    count = 0
    with expected.open("r", encoding="utf-8-sig", newline="") as left, actual.open(
        "r", encoding="utf-8-sig", newline=""
    ) as right:
        for line, (a, b) in enumerate(zip_longest(csv.reader(left), csv.reader(right)), 1):
            if a != b:
                raise ValueError(f"{expected.name}: {line}번째 CSV 행이 기준과 다릅니다.")
            count += 1
    return count - 1  # 헤더 제외


def run_season(season: str) -> dict:
    baseline = ROOT / "data" / "seasons" / season
    print(f"\n[{season}] [1/4] 원본 존재·SHA-256 검증", flush=True)
    raw_paths, expected = sources_for(season)
    print(f"  {len(raw_paths)}개 원본 검증 완료", flush=True)

    # 재생성 파일은 운영 경로가 아닌 시스템 임시 폴더에만 생성한다.
    with tempfile.TemporaryDirectory(prefix=f"jeju_auction_{season}_") as temporary:
        dest = Path(temporary)
        print(f"[{season}] [2/4] 거래 정제·일별 가격 생성", flush=True)
        pre = preprocess_auction(raw_paths, dest / "processed", dest / "reports")
        for field in ("input_rows", "accepted_rows", "rejected_rows", "observed_date_count", "daily_prices_rows"):
            if pre[field] != expected[field]:
                raise ValueError(f"{season}: {field} 불일치 (현재={pre[field]}, 기존={expected[field]})")

        print(f"[{season}] [3/4] 3·7·14일 동일 규격 가격쌍 생성", flush=True)
        pair = build_price_comparisons(dest / "processed" / "daily_prices.csv", dest / "processed", dest / "reports")
        for interval, expected_stats in expected["price_pairs"]["by_horizon"].items():
            stats = pair["by_interval"].get(int(interval), pair["by_interval"].get(interval))
            if stats is None or stats["matched_pairs"] != expected_stats["all_matched"] or \
                    stats["comparable_without_warnings"] != expected_stats["without_warnings"]:
                raise ValueError(f"{season}: {interval}일 연결쌍 집계가 기존 기록과 다릅니다.")

        print(f"[{season}] [4/4] 기준 CSV 4개와 전체 행 비교", flush=True)
        rows = {}
        for name in OUTPUTS:
            rows[name] = compare_csv(baseline / name, dest / "processed" / name)
            print(f"  PASS {name} ({rows[name]:,}행)", flush=True)

        return {
            "season": season,
            "status": "PASS",
            "raw_file_count": len(raw_paths),
            "raw_rows": pre["input_rows"],
            "accepted_rows": pre["accepted_rows"],
            "daily_price_rows": pre["daily_prices_rows"],
            "comparison_rows": pair["rows"],
            "csv_rows": rows,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="시즌 경매 데이터의 전체 파이프라인을 재현·검증합니다. 원본은 변경하지 않습니다.")
    parser.add_argument("--season", choices=(*SEASONS, "all"), default="all", help="검증할 시즌(기본: all)")
    parser.add_argument("--save-report", action="store_true", help="검증 요약 JSON 하나를 artifacts/에 저장")
    args = parser.parse_args()

    try:
        seasons = SEASONS if args.season == "all" else (args.season,)
        results = [run_season(season) for season in seasons]
        if args.save_report:
            output = ROOT / "artifacts" / "seunghyuk" / "시즌_파이프라인_검증.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            # 전체 시즌이 PASS인 경우에만 기존 검증 요약을 교체한다.
            temporary = output.with_suffix(".tmp")
            temporary.write_text(json.dumps({"status": "PASS", "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(output)
            print(f"검증 요약 저장: {output.relative_to(ROOT)}", flush=True)
    except (OSError, ValueError, KeyError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr, flush=True)
        return 1

    print(f"\n[PASS] {len(results)}개 시즌 원본→집계→가격쌍→내용 일치 검증 완료", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
