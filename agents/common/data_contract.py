"""시장 조건의 식별 키와 과거 가격 비교 자료의 버전 일치 여부를 검증합니다.

기존 Agent 또는 CSV를 수정하지 않는 읽기 전용 검증 계층입니다.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping


# 표시명이 같더라도 원천 코드가 다르면 동일 거래조건으로 합치지 않습니다.
MARKET_KEY_FIELDS = (
    "market_code", "variety_code", "grade_code", "size_code",
    "package", "package_kg", "trade_type",
)
PAIR_FIELDS = {
    "base_date", "target_date", "interval_days", *MARKET_KEY_FIELDS,
    "comparison_status", "unavailable_reason",
}
PRICE_FILE = Path("data/processed/daily_prices.csv")
PAIR_FILES = (
    Path("data/processed/price_comparisons.csv"),
    Path("data/processed/price_comparison_availability.csv"),
)
REPORT_FILE = Path("data/reports/price_comparison_report.json")
MANIFEST_FILE = Path("data/metadata/juneok_manifest.json")


def market_key(values: Mapping[str, object]) -> tuple[str, ...]:
    """원천 거래조건의 일곱 항목을 순서가 고정된 비교 키로 만듭니다."""
    if not isinstance(values, Mapping):
        raise ValueError("거래조건은 항목별 값이 있는 객체여야 합니다.")
    missing = [field for field in MARKET_KEY_FIELDS if field not in values]
    if missing:
        raise ValueError("거래조건 필수 항목 누락: " + ", ".join(missing))

    result: list[str] = []
    for field in MARKET_KEY_FIELDS:
        value = values[field]
        if field == "package_kg":
            if isinstance(value, bool) or value is None:
                raise ValueError("포장중량은 양수여야 합니다.")
            try:
                weight = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValueError("포장중량은 숫자여야 합니다.") from exc
            if not weight.is_finite() or weight <= 0:
                raise ValueError("포장중량은 유한한 양수여야 합니다.")
            result.append(format(weight.normalize(), "f"))
        else:
            # 코드의 선행 0을 유지하기 위해 숫자로 변환하지 않습니다.
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field}에는 비어 있지 않은 문자열이 필요합니다.")
            result.append(value.strip())
    return tuple(result)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("JSON 최상위 값은 객체여야 합니다.")
    return value


def _count_csv(path: Path, fields: set[str]) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        names = reader.fieldnames or []
        if len(names) != len(set(names)) or not fields.issubset(names):
            raise ValueError(f"CSV 컬럼이 계약과 다릅니다: {path.name}")
        count = 0
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV 행의 컬럼 수가 맞지 않습니다: {path.name}")
            count += 1
        return count


def _error(reason: str, message: str, **details: object) -> dict:
    return {"status": "DATA_ERROR", "reason": reason, "message": message, **details}


def verify_price_pair_sources(project_root: Path, market_root: Path | None = None) -> dict:
    """실제 선택된 시장가격 CSV와 과거 가격쌍 생성 원천이 동일한지 확인합니다.

    시장가격 스냅샷을 선택한 상태에서는 ``market_root``를 생략해도
    기존 Juneok의 검증된 활성 스냅샷을 조회합니다. 불일치 시 계산·조회에
    사용할 근거를 반환하지 않고 명확한 오류 상태로 종료합니다.
    """
    root = Path(project_root)
    try:
        if market_root is None:
            from agents.juneok.data_refresh import resolve_market_root
            selected_root = resolve_market_root(root)
        else:
            selected_root = Path(market_root)
        report = _read_json(root / REPORT_FILE)
        manifest = _read_json(root / MANIFEST_FILE)
        if (report.get("input_file") != PRICE_FILE.name
                or report.get("calendar_basis") != "calendar_days_exact_date"
                or report.get("pair_orientation") != "target_date = base_date + interval_days"
                or report.get("units") != {
                    "price": "KRW/kg", "weight": "kg",
                    "price_change_pct": "percent, not fraction",
                }):
            return _error("CONTRACT_MISMATCH", "과거 가격 비교 보고서의 원천·단위·날짜 규칙이 다릅니다.")

        intervals = report.get("interval_days")
        if (not isinstance(intervals, list) or not intervals
                or any(type(day) is not int or not 1 <= day <= 366 for day in intervals)
                or len(set(intervals)) != len(intervals)):
            return _error("CONTRACT_MISMATCH", "과거 가격 비교 간격 명세가 올바르지 않습니다.")

        original_sha = report.get("input_sha256")
        if not isinstance(original_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", original_sha):
            return _error("CONTRACT_MISMATCH", "과거 가격 비교 원천의 SHA-256 명세가 올바르지 않습니다.")
        entries = manifest.get("files")
        if not isinstance(entries, list):
            return _error("CONTRACT_MISMATCH", "공통 데이터 명세에서 파일 정보를 찾을 수 없습니다.")
        shared_entries = [x for x in entries if isinstance(x, dict) and x.get("file") == PRICE_FILE.as_posix()]
        if len(shared_entries) != 1 or shared_entries[0].get("sha256") != original_sha:
            return _error("CONTRACT_MISMATCH", "가격 비교 보고서와 공통 데이터 명세의 원천이 다릅니다.")

        shared_sha = _digest(root / PRICE_FILE)
        if shared_sha != original_sha:
            return _error("BASELINE_CHANGED", "공통 시장가격 CSV가 기록된 원천과 다릅니다.",
                          expected_sha256=original_sha, actual_sha256=shared_sha)
        selected_sha = _digest(selected_root / PRICE_FILE)
        if selected_sha != original_sha:
            return _error("VERSION_MISMATCH", "선택한 시장가격과 과거 가격 비교 자료의 원천 버전이 다릅니다.",
                          comparison_source_sha256=original_sha, selected_market_sha256=selected_sha)

        expected_rows = (report.get("rows"), report.get("availability_rows"))
        if any(type(n) is not int or n < 0 for n in expected_rows):
            return _error("CONTRACT_MISMATCH", "가격 비교 결과 행 수 명세가 올바르지 않습니다.")
        actual_rows = tuple(_count_csv(root / path, PAIR_FIELDS) for path in PAIR_FILES)
        if actual_rows != expected_rows:
            return _error("REPORT_COUNT_MISMATCH", "가격 비교 CSV와 보고서의 행 수가 다릅니다.",
                          expected_rows=expected_rows, actual_rows=actual_rows)

        return {
            "status": "OK",
            "reason": None,
            "message": "선택한 시장가격과 과거 가격 비교 자료의 원천이 일치합니다.",
            "comparison_source_sha256": original_sha,
            "selected_market_sha256": selected_sha,
            "available_intervals": intervals,
            "observed_first_date": report.get("observed_date_min"),
            "observed_last_date": report.get("observed_date_max"),
            "price_pair_rows": actual_rows[0],
            "availability_rows": actual_rows[1],
        }
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        # 원문 경로·입력 데이터 등은 사용자에게 그대로 전달하지 않습니다.
        return _error("SOURCE_READ_FAILED", "공통 데이터 또는 비교 자료를 검증하지 못했습니다.",
                      error_type=type(exc).__name__)


def verify_integrated_price_sources(project_root: Path, market_root: Path | None = None) -> dict:
    """가격 비교의 원천·보고서·각 결과 행을 전부 독립 재생성해 검증합니다.

    기존 ``verify_price_pair_sources``는 해시·컬럼·행 수만 검사합니다.
    실제 Agent에 가격 비교 도구를 연결할 때는 이 함수를 호출해야 합니다.
    CSV를 다시 생성해 디스크에 저장하지 않고, 메모리에서 비교합니다.
    """
    root = Path(project_root)
    basic = verify_price_pair_sources(root, market_root)
    if basic["status"] != "OK":
        return basic

    try:
        # 실행 중 활성 시장가격과 비교 원천이 달라질 수 있으므로 확인된
        # 동결 원천으로만 비교쌍을 다시 계산합니다.
        from scripts.build_price_comparisons import FIELDS, build_comparisons, load_prices

        observations, actual_sha = load_prices(root / PRICE_FILE)
        if actual_sha != basic["comparison_source_sha256"]:
            return _error("BASELINE_CHANGED", "검증 도중 공통 가격 원본이 변경됐습니다.")
        attempts, regenerated = build_comparisons(observations, basic["available_intervals"])
        report = _read_json(root / REPORT_FILE)

        # 단순한 행 수뿐 아니라 일자별·품종별 비교 집계 및 품질 사유가
        # 원천으로부터 산출한 결과와 같은지 확인합니다.
        for key, expected in regenerated.items():
            if report.get(key) != expected:
                return _error("REPORT_CONTENT_MISMATCH", "과거 가격 비교 집계가 원천 재계산 결과와 다릅니다.",
                              field=key)

        expected_files = (
            (PAIR_FILES[0], [r for r in attempts if r["comparison_status"] != "UNAVAILABLE"]),
            (PAIR_FILES[1], attempts),
        )
        for path, expected_rows in expected_files:
            with (root / path).open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != list(FIELDS):
                    return _error("PAIR_SCHEMA_MISMATCH", "가격 비교 CSV의 컬럼 계약이 다릅니다.",
                                  file=path.name)
                for line_number, expected in enumerate(expected_rows, start=2):
                    actual = next(reader, None)
                    regenerated_row = {field: str(expected[field]) for field in FIELDS}
                    if actual != regenerated_row:
                        return _error("PAIR_CONTENT_MISMATCH", "가격 비교 CSV가 원천 재계산 결과와 다릅니다.",
                                      file=path.name, line_number=line_number)
                if next(reader, None) is not None:
                    return _error("PAIR_CONTENT_MISMATCH", "가격 비교 CSV에 추가 행이 있습니다.",
                                  file=path.name)

        return {
            **basic,
            "message": "선택한 가격 원천, 과거 가격 비교 결과 및 보고서가 일치합니다.",
            "full_content_verified": True,
            "verified_comparison_rows": len(expected_files[0][1]),
            "verified_availability_rows": len(attempts),
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, ArithmeticError, csv.Error) as exc:
        # 경로나 원문 데이터·사용자 입력값을 메시지에 노출하지 않습니다.
        return _error("PAIR_REBUILD_FAILED", "가격 비교 원천 또는 결과를 재검증하지 못했습니다.",
                      error_type=type(exc).__name__)
