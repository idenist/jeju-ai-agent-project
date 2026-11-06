#!/usr/bin/env python3
"""동일 규격의 과거 경락가격을 3/7/14 달력일 간격으로 비교한다.

Python 표준 라이브러리만 사용. 원본·기존 정제 결과는 수정하지 않는다.
관측값이 없는 날을 보간하거나 휴장으로 추정하지 않는다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

KEY_FIELDS = (
    "market_code", "variety_code", "grade_code", "size_code",
    "package", "package_kg", "trade_type",
)
LABEL_FIELDS = ("market", "variety", "grade", "size")
REQUIRED = (
    "date", *KEY_FIELDS, *LABEL_FIELDS, "avg_price_kg", "total_kg",
    "total_amount_krw", "transaction_count", "quality_flag",
)
FIELDS = (
    "base_date", "target_date", "interval_days", *KEY_FIELDS, *LABEL_FIELDS,
    "base_price_kg", "target_price_kg", "price_change_krw_kg", "price_change_pct",
    "base_total_kg", "target_total_kg", "base_transaction_count",
    "target_transaction_count", "base_quality_flag", "target_quality_flag",
    "both_source_ok", "comparison_status", "comparison_warnings", "unavailable_reason",
)
UNKNOWN = {"", ".", "-", "null", "None", "미상"}
REASONS = {
    "OUTSIDE_OBSERVED_RANGE": "비교일이 이 시장의 정제 가격 관측기간 밖",
    "NO_MARKET_OBSERVATION_ON_TARGET_DATE": "비교일에 이 시장의 정제 가격 관측값 없음",
    "NO_MATCHING_CONDITION": "비교일에 이 시장의 가격은 있으나 동일 조건 없음",
}
WARNING_LABELS = {
    "LOW_SAMPLE": "한쪽 이상 거래 건수 5건 미만",
    "GRADE_UNKNOWN": "한쪽 이상 등급 미상",
    "SIZE_UNKNOWN": "한쪽 이상 크기 미상",
    "QUALITY_FLAG_MISSING": "품질 표시 누락",
    "KEY_FIELD_UNKNOWN": "비교 조건 일부 미상",
    "LABEL_MISMATCH": "같은 코드의 표시명이 날짜 간 다름",
}


def fixed(value: Decimal, digits: int) -> str:
    return format(value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), "f")


def positive_decimal(value: str, name: str, line: int) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{line}행: {name} 숫자 변환 불가") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{line}행: {name}은 유한한 양수여야 합니다")
    return result


def iso_date(value: str, line: int) -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except ValueError as exc:
        raise ValueError(f"{line}행: 날짜는 유효한 YYYY-MM-DD여야 합니다") from exc


@dataclass(frozen=True)
class Price:
    values: dict[str, str]
    day: date
    key: tuple
    price: Decimal
    kg: Decimal
    transactions: int
    warnings: frozenset[str]


def load_prices(path: Path) -> tuple[list[Price], str]:
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)):
        raise ValueError("입력 CSV 컬럼명이 중복됩니다")
    missing = sorted(set(REQUIRED) - set(headers))
    if missing:
        raise ValueError("필수 컬럼 누락: " + ", ".join(missing))
    prices = []
    seen = set()
    for line, record in enumerate(reader, start=2):
        if None in record or any(value is None for value in record.values()):
            raise ValueError(f"{line}행: CSV 컬럼 수 불일치")
        row = {name: record[name].strip() for name in REQUIRED}
        day = iso_date(row["date"], line)
        price = positive_decimal(row["avg_price_kg"], "avg_price_kg", line)
        kg = positive_decimal(row["total_kg"], "total_kg", line)
        amount = positive_decimal(row["total_amount_krw"], "total_amount_krw", line)
        package = positive_decimal(row["package_kg"], "package_kg", line)
        count = positive_decimal(row["transaction_count"], "transaction_count", line)
        if count != count.to_integral_value():
            raise ValueError(f"{line}행: 거래 건수는 양의 정수여야 합니다")
        if abs(price - amount / kg) > Decimal("0.01"):
            raise ValueError(f"{line}행: 가격과 금액/중량이 0.01원/kg 넘게 다릅니다")
        # 5, 5.0, 5.000은 같은 중량이다. 다른 조건은 임의로 통합하지 않는다.
        key = tuple(package if name == "package_kg" else row[name] for name in KEY_FIELDS)
        identity = (day, key)
        if identity in seen:
            raise ValueError(f"{line}행: 같은 날짜·비교조건이 중복됩니다")
        seen.add(identity)
        warnings = {v.strip() for v in row["quality_flag"].split(";") if v.strip() and v.strip() != "OK"}
        if not row["quality_flag"]:
            warnings.add("QUALITY_FLAG_MISSING")
        if row["grade"] in UNKNOWN or row["grade_code"] in UNKNOWN:
            warnings.add("GRADE_UNKNOWN")
        if row["size"] in UNKNOWN or row["size_code"] in UNKNOWN:
            warnings.add("SIZE_UNKNOWN")
        if any(row[name] in UNKNOWN for name in KEY_FIELDS if name != "package_kg"):
            warnings.add("KEY_FIELD_UNKNOWN")
        if count < 5:
            warnings.add("LOW_SAMPLE")
        prices.append(Price(row, day, key, price, kg, int(count), frozenset(warnings)))
    if not prices:
        raise ValueError("가격 관측 행이 없습니다")
    return sorted(prices, key=lambda p: (p.day, p.key)), hashlib.sha256(raw).hexdigest()


def validate_intervals(intervals) -> tuple[int, ...]:
    result = tuple(sorted(set(intervals)))
    if not result or any(type(n) is not int or n < 1 or n > 366 for n in result):
        raise ValueError("비교 간격은 1~366 사이 양의 정수여야 합니다")
    return result


def build_comparisons(prices: list[Price], intervals=(3, 7, 14)) -> tuple[list[dict], dict]:
    intervals = validate_intervals(intervals)
    index = {(p.day, p.key): p for p in prices}
    if len(index) != len(prices):
        raise ValueError("같은 날짜·비교조건이 중복됩니다")
    if not prices:
        raise ValueError("가격 관측 행이 없습니다")
    market_days: dict[str, set[date]] = {}
    for p in prices:
        market_days.setdefault(p.values["market_code"], set()).add(p.day)
    attempts = []
    for base in prices:
        for days in intervals:
            target_date = base.day + timedelta(days=days)
            target = index.get((target_date, base.key))
            row = {field: "" for field in FIELDS}
            row.update({field: base.values[field] for field in (*KEY_FIELDS, *LABEL_FIELDS)})
            row.update(
                base_date=base.day.isoformat(), target_date=target_date.isoformat(),
                interval_days=days, base_price_kg=fixed(base.price, 2),
                base_total_kg=fixed(base.kg, 3), base_transaction_count=base.transactions,
                base_quality_flag=base.values["quality_flag"], both_source_ok="false",
            )
            if target is None:
                observed = market_days[base.values["market_code"]]
                if target_date < min(observed) or target_date > max(observed):
                    reason = "OUTSIDE_OBSERVED_RANGE"
                elif target_date not in observed:
                    reason = "NO_MARKET_OBSERVATION_ON_TARGET_DATE"
                else:
                    reason = "NO_MATCHING_CONDITION"
                row.update(comparison_status="UNAVAILABLE", unavailable_reason=reason,
                           comparison_warnings=";".join(sorted(base.warnings)))
            else:
                warnings = set(base.warnings | target.warnings)
                if any(base.values[name] != target.values[name] for name in LABEL_FIELDS):
                    warnings.add("LABEL_MISMATCH")
                row.update(
                    target_price_kg=fixed(target.price, 2),
                    price_change_krw_kg=fixed(target.price - base.price, 2),
                    price_change_pct=fixed((target.price / base.price - 1) * 100, 6),
                    target_total_kg=fixed(target.kg, 3),
                    target_transaction_count=target.transactions,
                    target_quality_flag=target.values["quality_flag"],
                    both_source_ok=str(base.values["quality_flag"] == target.values["quality_flag"] == "OK").lower(),
                    comparison_status="COMPARABLE_WITH_WARNINGS" if warnings else "COMPARABLE",
                    comparison_warnings=";".join(sorted(warnings)),
                )
            attempts.append(row)

    def counts(rows):
        matched = [r for r in rows if r["comparison_status"] != "UNAVAILABLE"]
        return {
            "attempts": len(rows), "matched_pairs": len(matched),
            "both_source_ok_pairs": sum(r["both_source_ok"] == "true" for r in matched),
            "comparable_without_warnings": sum(r["comparison_status"] == "COMPARABLE" for r in matched),
            "matched_with_warnings": sum(r["comparison_status"] == "COMPARABLE_WITH_WARNINGS" for r in matched),
            "unavailable": len(rows) - len(matched),
            "unavailable_by_reason": dict(sorted(Counter(r["unavailable_reason"] for r in rows if r["comparison_status"] == "UNAVAILABLE").items())),
        }

    varieties = sorted({(p.values["variety_code"], p.values["variety"]) for p in prices})
    report = {
        "calendar_basis": "calendar_days_exact_date", "interval_days": list(intervals),
        "key_fields": list(KEY_FIELDS), "input_rows": len(prices),
        "distinct_condition_keys": len({p.key for p in prices}),
        "observed_date_min": min(p.day for p in prices).isoformat(),
        "observed_date_max": max(p.day for p in prices).isoformat(),
        "observed_dates_by_market": {k: [d.isoformat() for d in sorted(v)] for k, v in sorted(market_days.items())},
        "by_interval": {str(n): counts([r for r in attempts if r["interval_days"] == n]) for n in intervals},
        "by_variety": [
            {"variety_code": code, "variety": name,
             "input_rows": sum(p.values["variety_code"] == code and p.values["variety"] == name for p in prices),
             "by_interval": {str(n): counts([r for r in attempts if r["variety_code"] == code and r["variety"] == name and r["interval_days"] == n]) for n in intervals}}
            for code, name in varieties
        ],
        "unavailable_reason_labels": REASONS,
        "comparison_warning_labels": WARNING_LABELS,
        "limitations": [
            "과거 관측값 비교이며 미래 가격·보관 효과 예측이 아님",
            "날짜쌍은 기준일·규격이 겹칠 수 있어 독립 표본 수나 농가 수가 아님",
            "COMPARABLE은 현재 표시·규칙상 경고 없음이며 통계적 정확성 보장이 아님",
            "미관측 원인은 휴장·미수집·거래 없음 등으로 단정하지 않음",
            "거래정산일·경락가격은 농가 출하일·실수취액과 다름",
            "현재 수집기간·시장 밖으로 결과를 일반화하지 않음",
            "변동률은 정제 CSV에 표시된 소수점 둘째 자리 가격을 기준으로 계산",
        ],
    }
    return attempts, report


def coverage_markdown(report: dict, attempts: list[dict]) -> str:
    lines = [
        "# 동일 조건 가격 비교 가능 범위",
        "",
        f"- 입력: `{report['input_file']}` · SHA-256 `{report['input_sha256']}`",
        f"- 관측기간: {report['observed_date_min']} ~ {report['observed_date_max']}",
        f"- 상세 가격 {report['input_rows']:,}행 / 날짜를 제외한 비교조건 {report['distinct_condition_keys']:,}개",
        "- 달력일 기준 정확한 날짜만 연결. 중량 표기 5와 5.000은 같은 값으로 비교.",
        "",
        "## 전체 비교 범위",
        "",
        "| 간격 | 시도 수 | 연결된 날짜쌍 | 양쪽 원본 OK | 추가 점검 후 경고 없음 | 비교일·조건 없음 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for interval, stats in report["by_interval"].items():
        lines.append(f"| {interval}일 | {stats['attempts']:,} | {stats['matched_pairs']:,} | {stats['both_source_ok_pairs']:,} | {stats['comparable_without_warnings']:,} | {stats['unavailable']:,} |")
    lines += ["", "## 품종별 비교 범위", "", "| 품종 | 입력 행 | 간격 | 연결된 날짜쌍 | 양쪽 원본 OK | 추가 점검 후 경고 없음 |", "|---|---:|---|---:|---:|---:|"]
    for v in report["by_variety"]:
        for interval, stats in v["by_interval"].items():
            lines.append(f"| {v['variety']} ({v['variety_code']}) | {v['input_rows']:,} | {interval}일 | {stats['matched_pairs']:,} | {stats['both_source_ok_pairs']:,} | {stats['comparable_without_warnings']:,} |")
    lines += ["", "## 비교 불가 사유", "", "| 간격 | 사유 | 건수 |", "|---|---|---:|"]
    for interval, stats in report["by_interval"].items():
        for reason, count in stats["unavailable_by_reason"].items():
            lines.append(f"| {interval}일 | {REASONS[reason]} (`{reason}`) | {count:,} |")
    lines += [
        "", "## 7일 비교 시연 후보", "",
        "품종별로 경고 없는 날짜쌍 중 양쪽 최소 거래 건수가 큰 사례를 먼저 표시한다. 동률이면 양쪽 물량 합계와 날짜·규격 순으로 고른다. 저장 적합성이나 최적 시연 품종을 판정한 것은 아니다.",
        "", "| 품종 | 기준일 → 비교일 | 시장 / 등급 / 크기 / 포장 | 원/kg 변화 | 변동률 | 양쪽 거래 건수 |",
        "|---|---|---|---|---:|---|",
    ]
    candidates = sorted(
        (r for r in attempts if r["interval_days"] == 7 and r["comparison_status"] == "COMPARABLE"),
        key=lambda r: (-min(r["base_transaction_count"], r["target_transaction_count"]),
                       -(Decimal(r["base_total_kg"]) + Decimal(r["target_total_kg"])),
                       r["base_date"], tuple(str(r[k]) for k in KEY_FIELDS)),
    )
    seen = set()
    for r in candidates:
        if r["variety_code"] in seen:
            continue
        seen.add(r["variety_code"])
        lines.append(f"| {r['variety']} | {r['base_date']} → {r['target_date']} | {r['market']} / {r['grade']} / {r['size']} / {r['package']} {r['package_kg']}kg | {r['base_price_kg']} → {r['target_price_kg']} | {r['price_change_pct']}% | {r['base_transaction_count']} / {r['target_transaction_count']} |")
    if not seen:
        lines.append("| 해당 없음 | — | — | — | — | — |")
    lines += ["", "## 해석과 사용", ""]
    lines += ["- " + note for note in report["limitations"]]
    lines += [
        "- 불가 행의 비교가격·변동률은 빈 값이며 0%로 처리하지 않는다.",
        "- `price_comparisons.csv`에는 연결된 쌍만, `price_comparison_availability.csv`에는 모든 기준행×간격을 기록한다.",
        "- 에이전트의 기본 사례는 `comparison_status == COMPARABLE`로 조회한다. 경고가 있는 사례를 쓰면 이유를 함께 표시한다.",
        "- 같은 표준 품종코드·규격이어도 실제 품질과 거래 구성이 같다고 보장할 수 없다.",
        "- 휴장 여부는 이 파일로 확인하지 않았고 가장 가까운 날짜로 대체하지 않았다.",
        "",
    ]
    return "\n".join(lines)


def run(input_path: Path, out_dir: Path, report_dir: Path, intervals=(3, 7, 14)) -> dict:
    prices, digest = load_prices(input_path)
    attempts, report = build_comparisons(prices, intervals)
    report.update(input_file=input_path.name, input_sha256=digest,
                  units={"price": "KRW/kg", "weight": "kg", "price_change_pct": "percent, not fraction"},
                  pair_orientation="target_date = base_date + interval_days",
                  rows=sum(r["comparison_status"] != "UNAVAILABLE" for r in attempts),
                  availability_rows=len(attempts))
    paths = [out_dir / "price_comparisons.csv", out_dir / "price_comparison_availability.csv",
             report_dir / "price_comparison_report.json", report_dir / "price_comparison_coverage.md"]
    resolved = [p.resolve() for p in paths]
    if input_path.resolve() in resolved or len(set(resolved)) != len(resolved):
        raise ValueError("출력 경로가 입력 파일 또는 다른 출력과 겹칩니다")
    # 계산·검증을 모두 마친 뒤 출력한다.
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    for path, rows in [(paths[0], [r for r in attempts if r["comparison_status"] != "UNAVAILABLE"]), (paths[1], attempts)]:
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    paths[2].write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths[3].write_text(coverage_markdown(report, attempts), encoding="utf-8")
    return report


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=root / "data/processed/daily_prices.csv")
    parser.add_argument("--out-dir", type=Path, default=root / "data/processed")
    parser.add_argument("--report-dir", type=Path, default=root / "data/reports")
    parser.add_argument("--intervals", type=int, nargs="+", default=[3, 7, 14])
    args = parser.parse_args()
    try:
        result = run(args.input, args.out_dir, args.report_dir, args.intervals)
    except (OSError, ValueError, UnicodeError, InvalidOperation) as exc:
        print(f"[실패] {exc}", file=sys.stderr)
        return 1
    print(f"[완료] 입력 {result['input_rows']:,}행 → 연결 {result['rows']:,}쌍 / 전체 시도 {result['availability_rows']:,}행")
    for days, stats in result["by_interval"].items():
        print(f"  {days}일: {stats['matched_pairs']:,}쌍, 양쪽 원본 OK {stats['both_source_ok_pairs']:,}쌍, 추가 점검 후 경고 없음 {stats['comparable_without_warnings']:,}쌍")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
