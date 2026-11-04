#!/usr/bin/env python3
"""제주 감귤 경매원천정보(CSV) -> 일별 분석용 CSV. Python 3.9+, 외부 패키지 없음.

실행(프로젝트 루트에서): python scripts/preprocess_auction.py
경로 변경: python scripts/preprocess_auction.py --input 다른_원본.csv --out-dir data/processed

절대로 원본 CSV를 수정하지 않음. 판매단가 = 총거래금액 / 총거래중량(kg).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


VARIETY_BY_CODE = {
    "00": "감귤(일반)",
    "01": "조생귤",
    "04": "비가림감귤",
    "05": "하우스감귤",
    "06": "극조생감귤",
    "13": "온주",
    "19": "타이벡감귤",
    "99": "기타",
}

REQUIRED = (
    "trd_clcln_ymd", "whsl_mrkt_cd", "whsl_mrkt_nm", "gds_lclsf_cd",
    "gds_mclsf_cd", "gds_sclsf_cd", "gds_sclsf_nm", "corp_gds_vrty_nm",
    "plor_nm", "trd_se", "grd_cd", "grd_nm", "sz_cd", "sz_nm",
    "pkg_nm", "unit_nm", "unit_qty", "unit_tot_qty", "qty", "scsbd_prc", "totprc",
)

DETAILED_FIELDS = [
    "date", "market_code", "market", "variety_code", "variety",
    "grade_code", "grade", "size_code", "size", "package", "package_kg",
    "trade_type", "avg_price_kg", "total_kg", "total_amount_krw",
    "transaction_count", "quality_flag",
]
VARIETY_FIELDS = [
    "date", "market_code", "market", "variety_code", "variety", "trade_type",
    "avg_price_kg", "total_kg", "total_amount_krw", "transaction_count",
    "unknown_grade_pct", "unknown_size_pct", "quality_flag",
]

D0 = Decimal("0")
D1 = Decimal("1")


def text(value: object) -> str:
    return "" if value is None else str(value).strip()


def is_unknown(value: str) -> bool:
    return text(value) in ("", ".", "-", "null", "None", "미상")


def dec(value: str) -> Decimal:
    try:
        number = Decimal(text(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("숫자 변환 불가") from exc
    if not number.is_finite():
        raise ValueError("유한하지 않은 수")
    return number


def fixed(value: Decimal, digits: int) -> str:
    unit = Decimal(1).scaleb(-digits)
    return format(value.quantize(unit, rounding=ROUND_HALF_UP), "f")


def canonical_variety(code: str, label: str) -> str:
    # 같은 중분류(14) 안에서의 표준 소분류 코드. 법인별 자유입력 품종명은 사용하지 않음.
    return VARIETY_BY_CODE.get(code, text(label) or f"미분류({code})")


def quality_flags(*, transactions: int, grade: str | None = None,
                  size: str | None = None, variety_summary: bool = False) -> str:
    flags = []
    if variety_summary:
        flags.append("NOT_GRADE_SIZE_CONTROLLED")
    else:
        if grade == "미상":
            flags.append("GRADE_UNKNOWN")
        if size == "미상":
            flags.append("SIZE_UNKNOWN")
    if transactions < 5:
        flags.append("LOW_SAMPLE")
    return ";".join(flags) if flags else "OK"


def write_csv(path: Path, columns: list[str], records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Windows Excel에서도 한글 깨짐 없이 열리도록 utf-8-sig 사용.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)


def iter_records(input_files: list[Path]):
    """여러 시즌 CSV를 원본 변경 없이 순서대로 읽는다."""
    for input_file in input_files:
        if not input_file.is_file():
            raise FileNotFoundError(f"입력 파일을 찾을 수 없습니다: {input_file}")
        with input_file.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []
            missing = [col for col in REQUIRED if col not in headers]
            if missing:
                raise ValueError(f"{input_file.name}: 필수 컬럼 누락: " + ", ".join(missing))
            for record in reader:
                yield record, headers


def run(input_files: list[Path], out_dir: Path, report_dir: Path | None = None) -> dict:
    if not input_files:
        raise ValueError("입력 CSV가 최소 1개 필요합니다")

    drop_reasons: Counter[str] = Counter()
    issues: Counter[str] = Counter()
    included_varieties: Counter[str] = Counter()
    included_dates: Counter[str] = Counter()
    seen: set[bytes] = set()
    source_dates: Counter[str] = Counter()
    groups: dict[tuple[str, ...], dict] = defaultdict(lambda: {
        "kg": D0, "amount": D0, "count": 0,
    })
    daily: dict[tuple[str, ...], dict] = defaultdict(lambda: {
        "kg": D0, "amount": D0, "count": 0,
        "unknown_grade_kg": D0, "unknown_size_kg": D0,
    })
    input_rows = 0

    for record, headers in iter_records(input_files):
        input_rows += 1
        if text(record.get("trd_clcln_ymd")):
            source_dates[text(record["trd_clcln_ymd"])] += 1
        # 완전히 동일한 전체 레코드만 제거. 같은 가격·물량의 서로 다른 경매는 보존.
        fingerprint = hashlib.blake2b(
            "\x1f".join(text(record.get(col)) for col in headers).encode("utf-8"),
            digest_size=16,
        ).digest()
        if fingerprint in seen:
            drop_reasons["exact_duplicate"] += 1
            continue
        seen.add(fingerprint)

        if text(record["gds_lclsf_cd"]) != "06" or text(record["gds_mclsf_cd"]) != "14":
            drop_reasons["not_citrus"] += 1
            continue
        if not text(record["plor_nm"]).startswith("제주특별자치도"):
            drop_reasons["not_jeju_origin"] += 1
            continue
        if text(record["trd_se"]) != "경매":
            drop_reasons["not_auction"] += 1
            continue

        raw_date = text(record["trd_clcln_ymd"])
        try:
            valid_date = date.fromisoformat(raw_date)
            if valid_date.isoformat() != raw_date:
                raise ValueError("비표준 날짜")
        except ValueError:
            drop_reasons["invalid_date"] += 1
            continue

        try:
            qty = dec(record["qty"])
            price = dec(record["scsbd_prc"])
            amount = dec(record["totprc"])
            unit_qty = dec(record["unit_qty"])
            total_unit_qty = dec(record["unit_tot_qty"])
        except ValueError:
            drop_reasons["invalid_number"] += 1
            continue

        if min(qty, price, amount, unit_qty, total_unit_qty) <= D0:
            drop_reasons["nonpositive_qty_or_price"] += 1
            continue

        # 원본 단위가 명확하지 않은 거래는 kg으로 간주하지 않는다.
        unit = text(record["unit_nm"]).lower()
        if unit == "kg":
            multiplier = D1
        elif unit in {"g", "그램"}:
            multiplier = Decimal("0.001")
        elif unit in {"t", "ton", "톤"}:
            multiplier = Decimal("1000")
        else:
            drop_reasons["unsupported_weight_unit"] += 1
            continue

        # 엄격한 금액/중량 검증. 수정 거래, 잘못된 단위 정보는 집계에서 제외.
        if abs(qty * unit_qty - total_unit_qty) > Decimal("0.001"):
            drop_reasons["weight_mismatch"] += 1
            continue
        if abs(qty * price - amount) > D1:
            drop_reasons["amount_mismatch"] += 1
            continue

        kg = total_unit_qty * multiplier
        package_kg = unit_qty * multiplier
        if kg <= D0 or package_kg <= D0:
            drop_reasons["invalid_weight_kg"] += 1
            continue

        variety_code = text(record["gds_sclsf_cd"])
        variety = canonical_variety(variety_code, record["gds_sclsf_nm"])
        if variety_code not in VARIETY_BY_CODE:
            issues["unmapped_variety_code"] += 1
        grade = "미상" if is_unknown(record["grd_nm"]) else text(record["grd_nm"])
        size = "미상" if is_unknown(record["sz_nm"]) else text(record["sz_nm"])
        if grade == "미상":
            issues["grade_unknown_rows"] += 1
        if size == "미상":
            issues["size_unknown_rows"] += 1

        market_code = text(record["whsl_mrkt_cd"])
        market = text(record["whsl_mrkt_nm"])
        pkg = text(record["pkg_nm"]) or "미상"
        common = (raw_date, market_code, market, variety_code, variety)
        detail_key = common + (
            text(record["grd_cd"]), grade, text(record["sz_cd"]), size,
            pkg, fixed(package_kg, 3), "경매",
        )
        summary_key = common + ("경매",)

        group = groups[detail_key]
        group["kg"] += kg
        group["amount"] += amount
        group["count"] += 1

        day = daily[summary_key]
        day["kg"] += kg
        day["amount"] += amount
        day["count"] += 1
        if grade == "미상":
            day["unknown_grade_kg"] += kg
        if size == "미상":
            day["unknown_size_kg"] += kg

        included_varieties[variety] += 1
        included_dates[raw_date] += 1

    detail_rows = []
    for key, metrics in sorted(groups.items()):
        (dt, market_code, market, code, variety, grd_cd, grade,
         sz_cd, size, package, package_kg, trade_type) = key
        detail_rows.append(dict(zip(DETAILED_FIELDS, (
            dt, market_code, market, code, variety,
            grd_cd, grade, sz_cd, size, package, package_kg, trade_type,
            fixed(metrics["amount"] / metrics["kg"], 2),
            fixed(metrics["kg"], 3), fixed(metrics["amount"], 2),
            metrics["count"],
            quality_flags(transactions=metrics["count"], grade=grade, size=size),
        ))))

    summary_rows = []
    for key, metrics in sorted(daily.items()):
        dt, market_code, market, code, variety, trade_type = key
        total_kg = metrics["kg"]
        summary_rows.append(dict(zip(VARIETY_FIELDS, (
            dt, market_code, market, code, variety, trade_type,
            fixed(metrics["amount"] / total_kg, 2),
            fixed(total_kg, 3), fixed(metrics["amount"], 2),
            metrics["count"],
            fixed(metrics["unknown_grade_kg"] / total_kg * 100, 1),
            fixed(metrics["unknown_size_kg"] / total_kg * 100, 1),
            quality_flags(transactions=metrics["count"], variety_summary=True),
        ))))

    if report_dir is None:
        report_dir = out_dir.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    detail_csv = out_dir / "daily_prices.csv"
    summary_csv = out_dir / "daily_variety_prices.csv"
    report_path = report_dir / "auction_quality_report.json"
    write_csv(detail_csv, DETAILED_FIELDS, detail_rows)
    write_csv(summary_csv, VARIETY_FIELDS, summary_rows)

    accepted_count = sum(included_varieties.values())
    report = {
        "input_files": [p.name for p in input_files],
        "source": "전국 공영도매시장 경매원천정보 API (사용자가 수집한 CSV)",
        "input_rows": input_rows,
        "accepted_rows": accepted_count,
        "rejected_rows": sum(drop_reasons.values()),
        "rejected_by_primary_reason": dict(sorted(drop_reasons.items())),
        "non_excluding_warnings": dict(sorted(issues.items())),
        "source_dates": sorted(source_dates),
        "source_date_count": len(source_dates),
        "dates_without_accepted_auction": sorted(set(source_dates) - set(included_dates)),
        "observed_dates": sorted(included_dates),
        "observed_date_count": len(included_dates),
        "accepted_by_variety": dict(sorted(included_varieties.items())),
        "daily_prices_rows": len(detail_rows),
        "daily_variety_prices_rows": len(summary_rows),
        "filters": {
            "goods_code": "06/14 (과실류/감귤)",
            "origin": "plor_nm startswith 제주특별자치도",
            "trade_type": "경매 only",
            "price_unit": "원/kg",
            "price_formula": "sum(totprc) / sum(unit_tot_qty converted to kg)",
            "amount_validation": "abs(qty * scsbd_prc - totprc) <= 1 KRW",
            "weight_validation": "abs(qty * unit_qty - unit_tot_qty) <= 0.001 source unit",
            "unknown_grade_size": "kept as 미상, never imputed",
            "data_date_field": "trd_clcln_ymd (거래정산일자)",
        },
        "limitations": [
            "거래정산일은 농가의 실제 출하일과 다를 수 있음",
            "품종별 일별 평균은 등급·크기·포장 구성 변화의 영향을 받음",
            "경락가격은 생산자 실수취액이 아님",
            "2026년 10~11월 한 시장의 관측자료만으로 미래 가격을 예측할 수 없음",
        ],
    }
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return report


def cli() -> int:
    parser = argparse.ArgumentParser(description="감귤 경매 원본 -> 일별 가격 CSV 전처리")
    root = Path(__file__).resolve().parent.parent
    parser.add_argument("--input", type=Path, nargs="+", default=[root / "data/raw/auction_trades.csv"],
                        help="원본 CSV 경로(여러 개 가능). 기본: data/raw/auction_trades.csv")
    parser.add_argument("--out-dir", type=Path, default=root / "data/processed",
                        help="결과 저장 폴더 (기본: data/processed)")
    parser.add_argument("--report-dir", type=Path, default=root / "data/reports",
                        help="검증 보고서 저장 폴더 (기본: data/reports)")
    args = parser.parse_args()
    try:
        result = run(args.input, args.out_dir, args.report_dir)
    except (OSError, ValueError) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1

    print("[전처리 완료]")
    print(f"  입력={result['input_rows']:,}건, 분석 사용={result['accepted_rows']:,}건, 제외={result['rejected_rows']:,}건")
    print(f"  원본 날짜={result['source_date_count']}일, 분석가능 거래일={result['observed_date_count']}일, 상세 집계={result['daily_prices_rows']:,}행, 품종별 일별={result['daily_variety_prices_rows']:,}행")
    print(f"  제외 사유: {result['rejected_by_primary_reason']}")
    print(f"  파일: {args.out_dir / 'daily_prices.csv'}")
    print(f"        {args.out_dir / 'daily_variety_prices.csv'}")
    print(f"        {args.report_dir / 'auction_quality_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
