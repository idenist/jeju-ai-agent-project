"""같은 규격의 과거 가격 변동을 A·B 출하안의 B안 가정 단가로 바꾸는 도구. 추가 패키지 없이 실행한다."""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import median

from .price_history import (DEFAULT_FILE, FILTERS, ROOT, InputError, day, get_price_history,
                            number, read_rows)

PRICES_FILE = ROOT / "data/processed/daily_prices.csv"
REPORT_FILE = ROOT / "data/reports/price_comparison_report.json"
DATE_FIELDS = ("option_a_sale_date", "option_b_sale_date")
ALLOWED = {*FILTERS, *DATE_FIELDS, "option_a_price_per_kg"}
MANUAL_PRICE = "B안 단가를 직접 입력하면 손익 비교는 계속할 수 있습니다."
CAUTIONS = [
    "B안 단가는 과거 변동을 대입한 가정이며 가격 전망이 아닙니다.",
    "최솟값·최댓값은 각각 한 사례이고, 날짜 구간이 겹쳐 사례 수를 확률로 해석할 수 없습니다.",
    "중앙값은 사례 수가 짝수면 가운데 두 값의 평균이라 실제 한 사례가 아닐 수 있습니다.",
    "경락가격 기준이며 농가 실수취액이 아닙니다.",
]
SCENARIOS = (("min", "과거 가장 크게 내린 사례 적용"),
             ("median", "과거 변동 중앙값 적용"),
             ("max", "과거 가장 크게 오른 사례 적용"))


class DataVersionError(ValueError):
    """비교표와 가격 자료의 버전이 다를 때."""


AVAILABILITY_NAME = "price_comparison_availability.csv"


def _digests(raw: bytes) -> set[str]:
    # 원본 가격 해시는 생성 당시 바이트 그대로 기록돼 있다. Windows 체크아웃처럼 줄바꿈만 다르면 같은 자료로 본다.
    lf = raw.replace(b"\r\n", b"\n")
    return {hashlib.sha256(v).hexdigest() for v in (raw, lf, lf.replace(b"\n", b"\r\n"))}


def content_sha256(raw: bytes) -> str:
    # 비교표 해시는 build_price_comparisons.py와 같은 방식(LF로 맞춘 뒤 SHA-256)으로 계산한다.
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def check_source(prices_file: Path, report_file: Path, data_file: Path) -> dict:
    report = json.loads(Path(report_file).read_text(encoding="utf-8-sig"))
    expected = report.get("input_sha256")
    if not expected or expected not in _digests(Path(prices_file).read_bytes()):
        raise DataVersionError("비교표 보고서가 현재 가격 자료와 다른 버전입니다. 비교표를 다시 생성해 주세요.")
    # 실제로 읽는 비교표도 보고서와 같은 생성본인지 확인한다. 이 파일만 바뀌어도 계산하지 않는다.
    table = (report.get("output_sha256") or {}).get(AVAILABILITY_NAME)
    if not table or table != content_sha256(Path(data_file).read_bytes()):
        raise DataVersionError("비교표 파일이 보고서와 다른 버전입니다. 비교표를 다시 생성해 주세요.")
    return {"price_file": report.get("input_file"), "price_sha256": expected,
            "comparison_sha256": table, "hash_basis": report.get("output_hash_basis"),
            "observed_period": [report.get("observed_date_min"), report.get("observed_date_max")]}


def won(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), ROUND_HALF_UP))


def get_price_scenarios(args: dict, data_file: Path = DEFAULT_FILE, prices_file: Path = PRICES_FILE,
                        report_file: Path = REPORT_FILE) -> dict:
    if not isinstance(args, dict) or set(args) - ALLOWED:
        raise InputError("도구 입력은 허용된 필드의 객체여야 합니다.")
    common = {"is_future_forecast": False, "cautions": CAUTIONS}
    missing = [k for k in DATE_FIELDS if args.get(k) in (None, "")]
    if missing:
        return {**common, "status": "NEEDS_CLARIFICATION", "missing_fields": missing, "choices": {},
                "message": "A안과 B안의 예상 판매일을 알려 주세요."}
    try:
        a_day, b_day = day(args["option_a_sale_date"]), day(args["option_b_sale_date"])
    except (TypeError, ValueError) as exc:
        raise InputError("예상 판매일은 실제로 존재하는 YYYY-MM-DD 형식이어야 합니다.") from exc
    if b_day <= a_day:
        raise InputError("B안 예상 판매일은 A안보다 늦어야 합니다.")
    a_price = args.get("option_a_price_per_kg")
    if a_price is not None:
        try:
            a_price = number(a_price)
        except ValueError as exc:
            raise InputError("A안 단가(원/kg)를 숫자로 입력해 주세요.") from exc
        if a_price <= 0:
            raise InputError("A안 단가(원/kg)는 0보다 커야 합니다.")

    source = check_source(prices_file, report_file, data_file)
    interval = (b_day - a_day).days
    history = get_price_history({**{k: args[k] for k in FILTERS if k in args},
                                 "interval_days": interval, "limit": 3}, data_file)
    options = {"option_a": {"sale_date": a_day.isoformat(),
                            "price_per_kg": None if a_price is None else str(a_price),
                            "price_type": "assumption", "price_source": "사용자 입력"},
               "option_b": {"sale_date": b_day.isoformat()}}
    common.update(interval_days=interval, source={**source, "comparison_file": history.get("source")},
                  applied_defaults=history.get("applied_defaults", {}),
                  normalized_conditions=history.get("normalized_conditions", {}), **options)

    status = history["status"]
    if status == "INTERVAL_NOT_AVAILABLE":
        days = ", ".join(f"{d}일" for d in history["available_intervals"])
        return {**common, "status": status, "available_intervals": history["available_intervals"],
                "manual_price_allowed": True,
                "message": f"두 판매일 간격 {interval}일의 과거 비교는 없습니다({days}만 가능). {MANUAL_PRICE}"}
    if status == "NEEDS_CLARIFICATION":
        return {**common, "status": status, "missing_fields": history["missing_fields"],
                "choices": history["choices"], "message": history["message"]}
    if status != "OK":
        # 같은 조건의 기록이 없거나 경고 없는 사례가 0건이면 가정 단가를 만들지 않는다.
        keep = {k: history[k] for k in ("clean_pairs", "warning_pairs", "unavailable_pairs",
                                         "unavailable_reasons") if k in history}
        return {**common, **keep, "status": status, "manual_price_allowed": True,
                "message": f"{history.get('message', '비교할 과거 사례가 없습니다.')} {MANUAL_PRICE}"}

    # 화면 예시(최근 몇 쌍)가 아니라 경고 없는 전체 사례로 통계를 낸다.
    conditions = history["conditions"]
    clean = [r for r in read_rows(Path(data_file))
             if r["comparison_status"] == "COMPARABLE" and int(r["interval_days"]) == interval
             and all((number(r[k]) == number(v)) if k == "package_kg" else r[k] == v
                     for k, v in conditions.items())]
    if len(clean) != history["clean_pairs"]:
        raise ValueError("과거 사례 수가 조회 결과와 맞지 않습니다.")
    changes = sorted(number(r["price_change_pct"]) for r in clean)
    stats = {"min": changes[0], "median": median(changes), "max": changes[-1]}
    scenarios = [{"basis": key, "label": label, "change_pct": won(stats[key]), "price_type": "assumption",
                  "option_b_price_per_kg": None if a_price is None
                  else won(a_price * (1 + stats[key] / 100))}
                 for key, label in SCENARIOS]
    # 통합 응답 규격이 확인하는 필드(conditions, 쌍 개수, examples)는 정현 조회 도구와 같은 위치에 둔다.
    result = {**common, "status": "OK", "conditions": conditions,
              **{k: history[k] for k in ("clean_pairs", "warning_pairs", "unavailable_pairs",
                                         "unavailable_reasons", "examples")},
              "base_date_range": [min(r["base_date"] for r in clean), max(r["base_date"] for r in clean)],
              "scenarios": scenarios}
    if a_price is None:
        result.update(status="NEEDS_CLARIFICATION", missing_fields=["option_a_price_per_kg"], choices={},
                      message="B안 가정 단가를 계산하려면 A안 단가(원/kg)를 알려 주세요.")
    return result


def execute_scenario_tool(args: dict, **paths) -> dict:
    try:
        return get_price_scenarios(args, **paths)
    except InputError as exc:
        return {"status": "ERROR", "error_kind": "INVALID_INPUT", "error": str(exc)}
    except DataVersionError as exc:
        return {"status": "ERROR", "error_kind": "DATA_ERROR", "error": str(exc)}
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
        return {"status": "ERROR", "error_kind": "DATA_ERROR", "error": "비교 데이터 파일을 확인해 주세요."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="조회 조건 JSON")
    opts = parser.parse_args()
    try:
        request = json.loads(opts.request)
    except json.JSONDecodeError:
        print("유효한 JSON을 입력해 주세요.")
        return 1
    result = execute_scenario_tool(request)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] == "ERROR")


if __name__ == "__main__":
    raise SystemExit(main())
