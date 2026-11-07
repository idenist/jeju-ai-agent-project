"""과거 가격 비교 조회 도구. 추가 패키지나 API 키 없이 실행한다."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILE = ROOT / "data/processed/price_comparison_availability.csv"
FILTERS = ("market_code", "variety", "grade", "size", "package", "package_kg", "trade_type")
KEYS = ("market_code", "variety_code", "grade_code", "size_code", "package", "package_kg", "trade_type")
STATUSES = {"COMPARABLE", "COMPARABLE_WITH_WARNINGS", "UNAVAILABLE"}
REASONS = {
    "OUTSIDE_OBSERVED_RANGE": "비교일이 확보한 가격 관측기간 밖입니다.",
    "NO_MARKET_OBSERVATION_ON_TARGET_DATE": "비교일에 해당 시장의 정제 가격 자료가 없습니다.",
    "NO_MATCHING_CONDITION": "비교일에 동일 조건의 가격 자료가 없습니다.",
}
CAUTIONS = [
    "과거 관측값 비교이며 미래 가격이나 보관 수익 예측이 아닙니다.",
    "날짜 구간이 겹칠 수 있어 사례 수를 독립 표본이나 미래 확률로 해석할 수 없습니다.",
    "같은 분류의 서로 다른 거래를 비교하며 동일 감귤을 보관한 결과가 아닙니다.",
    "경락가격은 농가 실수취액이 아닙니다. 저장 적합성은 별도 확인이 필요합니다.",
]


class InputError(ValueError):
    """사용자가 조회 조건을 고쳐 해결할 수 있는 오류."""


def number(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("유효한 숫자가 필요합니다.") from exc
    if not result.is_finite():
        raise ValueError("유한한 숫자가 필요합니다.")
    return result


def day(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    return parsed


def read_rows(path: Path) -> list[dict]:
    required = {*FILTERS, *KEYS, "interval_days", "base_date", "target_date",
                "comparison_status", "comparison_warnings", "unavailable_reason",
                "base_price_kg", "target_price_kg", "price_change_pct"}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("비교 CSV 필수 컬럼이 누락됐습니다.")
        rows = list(reader)
    seen = set()
    for row in rows:
        if None in row or any(v is None for v in row.values()):
            raise ValueError("비교 CSV 컬럼 수가 맞지 않습니다.")
        interval = int(row["interval_days"])
        if not 1 <= interval <= 366:
            raise ValueError("비교 CSV의 간격이 잘못됐습니다.")
        if day(row["target_date"]) != day(row["base_date"]) + timedelta(days=interval):
            raise ValueError("비교 CSV 날짜와 간격이 일치하지 않습니다.")
        kg = number(row["package_kg"])
        if kg <= 0:
            raise ValueError("포장중량은 양수여야 합니다.")
        key = (row["base_date"], interval, *(kg if k == "package_kg" else row[k] for k in KEYS))
        if key in seen:
            raise ValueError("비교 CSV에 동일 날짜·간격·조건 중복이 있습니다.")
        seen.add(key)
        status = row["comparison_status"]
        if status not in STATUSES:
            raise ValueError("알 수 없는 비교 상태입니다.")
        if status == "UNAVAILABLE":
            if row["unavailable_reason"] not in REASONS:
                raise ValueError("알 수 없는 비교 불가 사유입니다.")
        else:
            if number(row["base_price_kg"]) <= 0 or number(row["target_price_kg"]) <= 0:
                raise ValueError("비교 가격은 양수여야 합니다.")
            number(row["price_change_pct"])
            if status == "COMPARABLE" and row["comparison_warnings"]:
                raise ValueError("비교 상태와 경고가 일치하지 않습니다.")
    return rows


def get_price_history(args: dict, data_file: Path = DEFAULT_FILE) -> dict:
    allowed = {*FILTERS, "interval_days", "base_date", "limit"}
    if not isinstance(args, dict) or set(args) - allowed:
        raise InputError("도구 입력은 허용된 필드의 객체여야 합니다.")
    interval = args.get("interval_days")
    if interval is not None and (type(interval) is not int or not 1 <= interval <= 366):
        raise InputError("비교 간격은 1~366 사이 정수로 입력해 주세요.")
    limit = args.get("limit", 5)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise InputError("표시 개수는 1~20의 정수여야 합니다.")
    base_date = args.get("base_date")
    if base_date is not None:
        try:
            day(base_date)
        except (TypeError, ValueError) as exc:
            raise InputError("날짜는 실제로 존재하는 YYYY-MM-DD 형식이어야 합니다.") from exc
    supplied = {}
    for field in FILTERS:
        value = args.get(field)
        if value is None or value == "":
            continue
        if field == "package_kg":
            try:
                value = number(value)
            except ValueError as exc:
                raise InputError("포장 중량(kg)을 숫자로 입력해 주세요.") from exc
            if value <= 0:
                raise InputError("포장 중량(kg)은 0보다 커야 합니다.")
        elif not isinstance(value, str) or not value.strip():
            raise InputError(f"{field} 조건에 값을 입력해 주세요.")
        else:
            value = value.strip()
        supplied[field] = value
    rows = read_rows(Path(data_file))
    try:
        source = Path(data_file).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        source = Path(data_file).name
    applied_defaults = {}
    # 시장과 거래방식만 자료 전체에서 유일할 때 적용하고, 사용자 지정값은 바꾸지 않는다.
    for field in ("market_code", "trade_type"):
        values = {r[field] for r in rows}
        if field not in supplied and len(values) == 1:
            value = next(iter(values))
            if value.strip() and value not in {".", "-", "미상"}:
                supplied[field] = value
                applied_defaults[field] = value
    common = {"is_future_forecast": False, "source": source, "cautions": CAUTIONS,
              "applied_defaults": applied_defaults}
    if "market_code" in applied_defaults:
        names = sorted({r.get("market", "") for r in rows} - {""})
        common["applied_defaults_note"] = "시장: " + (" / ".join(names) or applied_defaults["market_code"])
    if "trade_type" in applied_defaults:
        common["applied_defaults_note"] = (common.get("applied_defaults_note", "") + " 거래방식: " + applied_defaults["trade_type"]).strip()
    supported_intervals = {int(r["interval_days"]) for r in rows}
    if interval is not None and interval not in supported_intervals:
        return {**common, "status": "INTERVAL_NOT_AVAILABLE", "available_intervals": sorted(supported_intervals),
                "message": "요청한 간격의 비교 자료가 없습니다. 확보된 간격을 확인해 주세요."}
    candidates = [r for r in rows if all(
        (number(r[k]) if k == "package_kg" else r[k]) == v for k, v in supplied.items())]
    missing = [k for k in FILTERS if k not in supplied]
    if interval is None:
        missing.append("interval_days")
    # 빠진 조건을 임의로 채우지 않고 후보를 반환해 사용자에게 확인한다.
    if missing:
        # 같은 관측행이 여러 비교 간격에 반복돼도 보기의 빈도는 한 번만 센다.
        observations = {
            (r["base_date"], *(r[k] for k in KEYS)): r for r in candidates
        }
        choices = {}
        for key in missing:
            if key not in FILTERS:
                continue
            values = Counter(
                format(number(r[key]).normalize(), "f") if key == "package_kg" else r[key]
                for r in observations.values() if r[key].strip()
            )
            choices[key] = sorted(values, key=lambda v: (v in {"미상", ".", "-"}, -values[v], v))
        if "interval_days" in missing:
            choices["interval_days"] = sorted(supported_intervals)
        return {**common, "status": "NEEDS_CLARIFICATION", "missing_fields": missing,
                "choices": choices, "message": "빠진 비교 조건을 확인해 주세요."}
    selected = [r for r in candidates if int(r["interval_days"]) == interval
                and (base_date is None or r["base_date"] == base_date)]
    if not selected:
        return {**common, "status": "NO_DATA", "message": "지정한 조건·기준일의 기록이 없습니다. 조건을 확인해 주세요."}
    identities = {tuple(number(r[k]) if k == "package_kg" else r[k] for k in KEYS) for r in selected}
    if len(identities) != 1:
        return {**common, "status": "AMBIGUOUS_CODES", "message": "같은 표시명에 여러 코드가 있습니다. 코드 대응을 확인해 주세요."}
    counts = Counter(r["comparison_status"] for r in selected)
    reasons = Counter(r["unavailable_reason"] for r in selected if r["comparison_status"] == "UNAVAILABLE")
    warnings = Counter(w for r in selected for w in r["comparison_warnings"].split(";") if w)
    clean = sorted((r for r in selected if r["comparison_status"] == "COMPARABLE"),
                   key=lambda r: r["base_date"], reverse=True)
    changes = [number(r["price_change_pct"]) for r in clean]
    # 경고가 있는 쌍은 기본 사례와 변동 범위에서 제외하되, 제외 사실은 숨기지 않는다.
    return {**common, "status": "OK" if clean else "NO_CLEAN_COMPARISON",
            "conditions": {k: str(v) for k, v in supplied.items()},
            "interval_days": interval, "base_date_filter": base_date,
            "attempts": len(selected), "clean_pairs": len(clean),
            "warning_pairs": counts["COMPARABLE_WITH_WARNINGS"],
            "unavailable_pairs": counts["UNAVAILABLE"],
            "unavailable_reasons": [{"code": k, "message": REASONS[k], "count": v} for k, v in sorted(reasons.items())],
            "warning_counts": dict(sorted(warnings.items())),
            "clean_change_range_pct": {"min": str(min(changes)), "max": str(max(changes))} if changes else None,
            "examples": clean[:limit], "displayed_examples": min(len(clean), limit),
            "example_order": "기준일 최신순", "truncated": len(clean) > limit}


def execute_tool(name: str, args: dict, data_file: Path = DEFAULT_FILE) -> dict:
    if name != "get_price_history":
        return {"status": "ERROR", "error": "등록되지 않은 도구입니다."}
    try:
        return get_price_history(args, data_file)
    except InputError as exc:
        return {"status": "ERROR", "error_kind": "INVALID_INPUT", "error": str(exc)}
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
        return {"status": "ERROR", "error_kind": "DATA_ERROR", "error": "비교 데이터 파일을 확인해 주세요."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="조회 조건 JSON")
    parser.add_argument("--data-file", type=Path, default=DEFAULT_FILE)
    opts = parser.parse_args()
    try:
        request = json.loads(opts.request)
    except json.JSONDecodeError:
        print("유효한 JSON을 입력해 주세요.")
        return 1
    result = execute_tool("get_price_history", request, opts.data_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] == "ERROR")


if __name__ == "__main__":
    raise SystemExit(main())
