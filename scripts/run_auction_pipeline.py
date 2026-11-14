"""시즌 경매 원본을 독립 재생성하고 기존 결과와 비교하는 단일 실행 진입점.

기존 전처리·가격쌍 생성 함수를 그대로 사용하며, data/ 운영 파일은 수정하지 않는다.
기본 실행 시 임시 폴더의 결과는 검증 직후 삭제한다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path

from scripts.build_price_comparisons import FIELDS as PAIR_FIELDS, run as build_price_comparisons
from scripts.preprocess_auction import (
    DETAILED_FIELDS, VARIETY_FIELDS, run as preprocess_auction,
)

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



def csv_row_count(path: Path, expected_fields: list[str] | tuple[str, ...]) -> int:
    """서비스 계약과 동일한 컬럼 순서·행 구조인지 확인한다."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(expected_fields):
            raise ValueError(f"{path.name}: 컬럼 계약 불일치")
        count = 0
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path.name}: {count + 2}행의 컬럼 수 불일치")
            count += 1
        return count


def build_new_dataset(inputs: list[Path], name: str) -> dict:
    """신규 원본을 운영 파일과 분리된 검증 묶음으로 생성한다. 자동 활성화하지 않는다."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("데이터 이름은 영문/숫자로 시작하는 영문·숫자·밑줄·하이픈 1~64자여야 합니다.")
    sources = [p.resolve(strict=True) for p in inputs]
    if len(sources) != len(set(sources)) or not all(p.is_file() and p.suffix.lower() == ".csv" for p in sources):
        raise ValueError("중복 없는 CSV 원본 파일만 입력할 수 있습니다.")

    base = ROOT / "artifacts" / "seunghyuk" / "datasets"
    destination = base / name
    if destination.exists():
        raise ValueError(f"같은 이름의 데이터 묶음이 이미 존재합니다: {name} (덮어쓰기 금지)")
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=base))
    try:
        raw_dir = staging / "data" / "raw"
        processed = staging / "data" / "processed"
        reports = staging / "data" / "reports"
        raw_dir.mkdir(parents=True)
        print(f"[{name}] [1/4] 원본 보존·해시 검증", flush=True)
        copied = []
        input_metadata = []
        for i, source in enumerate(sources, 1):
            raw = raw_dir / f"source_{i:02d}.csv"
            before = sha256(source)
            shutil.copyfile(source, raw)
            if sha256(raw) != before or sha256(source) != before:
                raise ValueError(f"원본 복사 중 파일이 변경됐습니다: {source.name}")
            copied.append(raw)
            input_metadata.append({
                "original_filename": source.name,
                "file": raw.relative_to(staging).as_posix(),
                "sha256": before,
                "bytes": raw.stat().st_size,
            })
        print(f"[{name}] [2/4] 경매 거래 정제·일별 가격 생성", flush=True)
        pre = preprocess_auction(copied, processed, reports)
        if pre["input_rows"] <= 0 or pre["accepted_rows"] <= 0 or pre["daily_prices_rows"] <= 0:
            raise ValueError("원본 거래 또는 분석 가능한 경매가격이 없습니다.")
        if pre["input_rows"] != pre["accepted_rows"] + pre["rejected_rows"]:
            raise ValueError("채택·제외 거래 합계가 원본 거래 건수와 다릅니다.")
        today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        dates = pre["observed_dates"]
        if not dates or dates[-1] > today:
            raise ValueError("유효한 관측일이 없거나 미래 거래일이 포함됐습니다.")

        print(f"[{name}] [3/4] 날짜·동일 규격 가격쌍 생성", flush=True)
        pair = build_price_comparisons(processed / "daily_prices.csv", processed, reports)
        if pair["input_rows"] != pre["daily_prices_rows"] or pair["input_sha256"] != sha256(processed / "daily_prices.csv"):
            raise ValueError("가격쌍과 일별 가격 원천의 버전이 일치하지 않습니다.")
        if pair["availability_rows"] != pre["daily_prices_rows"] * len(pair["interval_days"]):
            raise ValueError("가격 비교 전체 시도 행 수가 입력과 일치하지 않습니다.")
        if pair["rows"] != sum(stats["matched_pairs"] for stats in pair["by_interval"].values()):
            raise ValueError("가격 비교 연결쌍 집계가 일치하지 않습니다.")

        print(f"[{name}] [4/4] CSV 계약·행 수·해시 검증 후 묶음 확정", flush=True)
        expected = (
            ("daily_prices.csv", DETAILED_FIELDS, pre["daily_prices_rows"]),
            ("daily_variety_prices.csv", VARIETY_FIELDS, pre["daily_variety_prices_rows"]),
            ("price_comparisons.csv", PAIR_FIELDS, pair["rows"]),
            ("price_comparison_availability.csv", PAIR_FIELDS, pair["availability_rows"]),
        )
        counts = {}
        for filename, fields, count in expected:
            actual = csv_row_count(processed / filename, fields)
            if actual != count:
                raise ValueError(f"{filename}: 보고서 {count}행 / 실제 {actual}행")
            counts[filename] = actual

        files = [*copied, *(processed / filename for filename in OUTPUTS),
                 reports / "auction_quality_report.json",
                 reports / "price_comparison_report.json",
                 reports / "price_comparison_coverage.md"]
        hashes = {p.relative_to(staging).as_posix(): sha256(p) for p in files}
        clean = sum(x["comparable_without_warnings"] for x in pair["by_interval"].values())
        manifest = {
            "status": "OFFLINE_VALIDATED",
            "agent_activated": False,
            "snapshot_name": name,
            "observed_date_min": dates[0],
            "observed_date_max": dates[-1],
            "input_rows": pre["input_rows"],
            "accepted_rows": pre["accepted_rows"],
            "rejected_rows": pre["rejected_rows"],
            "csv_rows": counts,
            "clean_price_pairs": clean,
            "price_pair_usable": clean > 0,
            "inputs": input_metadata,
            "sha256": hashes,
            "notes": [
                "원본 CSV는 동일 묶음의 data/raw/에 복사·보존되었습니다.",
                "가격 비교는 과거 관측값이며 미래가격 예측이나 출하 효과가 아닙니다.",
                "Agent 연결·기본 데이터 전환에는 팀 데이터 계약 및 승인 절차가 필요합니다.",
            ],
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if destination.exists():
            raise ValueError("데이터 이름이 처리 도중 중복됐습니다. 기존 결과를 덮어쓰지 않습니다.")
        staging.rename(destination)
        print(f"  검증 완료: 거래 {pre['accepted_rows']:,}건, 가격 {pre['daily_prices_rows']:,}행, 경고 없는 쌍 {clean:,}개", flush=True)
        if clean == 0:
            print("  [주의] 정상 가격쌍이 없어 과거 가격 비교에는 사용할 수 없습니다.", flush=True)
        print(f"  저장: {destination.relative_to(ROOT)}", flush=True)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(description="시즌 재현성 검증 또는 신규 원본의 오프라인 데이터 묶음 생성")
    parser.add_argument("--season", choices=(*SEASONS, "all"), help="기존 시즌 검증 (기본: all)")
    parser.add_argument("--save-report", action="store_true", help="기존 시즌 검증 요약을 artifacts/에 저장")
    parser.add_argument("--input", type=Path, nargs="+", help="신규 경매 원본 CSV (여러 개 가능)")
    parser.add_argument("--name", help="신규 데이터 묶음 이름. 이미 존재하면 덮어쓰지 않음")
    args = parser.parse_args()

    if args.input and (args.season is not None or args.save_report or not args.name):
        parser.error("신규 데이터 모드에는 --input CSV... --name 이름만 사용하세요.")
    if not args.input and args.name:
        parser.error("--name을 사용할 때는 --input을 지정하세요.")
    try:
        if args.input:
            build_new_dataset(args.input, args.name)
            print("\n[PASS] 신규 원본 → 오프라인 검증 데이터 묶음 생성 완료 (Agent에는 미반영)", flush=True)
            return 0
        seasons = SEASONS if args.season in (None, "all") else (args.season,)
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
