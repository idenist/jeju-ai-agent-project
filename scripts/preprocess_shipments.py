#!/usr/bin/env python3
"""제주데이터허브 #855 출하량 전처리 (Python 표준 라이브러리만 사용).

입력:  data/raw/jeju_shipments.csv (원본은 수정하지 않음)
출력:  data/processed/daily_shipments.csv
       data/processed/monthly_shipments.csv
       data/reports/shipment_unassigned_rows.csv
       data/reports/shipment_quality_report.json

사용: python scripts/preprocess_shipments.py
      python scripts/preprocess_shipments.py --input data/raw/jeju_shipments.csv
"""

from __future__ import annotations

import argparse
import calendar
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

# 제주데이터허브 제공 원본의 5개 컬럼명. 경매가격 표와는 분류체계·단위가 다르다.
SOURCE_COLS = ["출하일자", "품종대분류", "품종소분류", "신고소", "출하량"]
DAILY_COLS = [
    "date", "category", "variety", "shipment_tons", "reporting_offices",
    "positive_offices", "source_rows", "data_warning",
]
MONTHLY_COLS = [
    "year_month", "category", "variety", "shipment_tons", "observed_days",
    "source_rows", "data_warning",
]
UNASSIGNED_COLS = ["exclusion_reason"] + SOURCE_COLS
TON_SCALE = Decimal("0.01")


# 사용자 환경에 따라 CP949·UTF-8로 저장될 수 있으므로 인코딩을 순서대로 시도한다.
def read_source(path: Path) -> tuple[list[dict[str, str]], str]:
    if not path.is_file():
        raise FileNotFoundError(f"입력 파일이 없습니다: {path}")
    raw = path.read_bytes()
    decoded = None
    selected_encoding = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            decoded = raw.decode(encoding)
            selected_encoding = encoding
            break
        except UnicodeDecodeError:
            pass
    if decoded is None or selected_encoding is None:
        raise ValueError(f"UTF-8 / CP949로 읽을 수 없습니다: {path}")

    # 컬럼 수가 어긋난 행을 조용히 건너뛰면 집계가 틀어지므로 오류로 중단한다.
    reader = csv.DictReader(io.StringIO(decoded, newline=""))
    if reader.fieldnames is None or not set(SOURCE_COLS).issubset(reader.fieldnames):
        raise ValueError(f"필수 컬럼 누락: {path}; 필요한 컬럼={SOURCE_COLS}")
    rows = []
    for line_num, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"CSV 컬럼 개수가 맞지 않습니다: {path}:{line_num}")
        rows.append({col: (row.get(col) or "").strip() for col in SOURCE_COLS})
    return rows, selected_encoding


# 출하량의 기본 단위는 톤이며, 사용자에게 보이는 합계는 소수 둘째 자리로 나타낸다.
def ton_str(value: Decimal) -> str:
    return format(value.quantize(TON_SCALE), "f")


# 음수·비정상 숫자를 유효 출하량으로 합치지 않는다. 원본의 명시적 0톤은 허용한다.
def parse_tons(value: str) -> Decimal | None:
    try:
        amount = Decimal(value.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def save_csv(path: Path, fields: list[str], records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 BOM: Windows Excel에서도 한글이 깨지지 않도록 저장
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


# 전체 파이프라인: 인코딩 확인 → 날짜·중량 검증 → 중복 확인 → 일·월별 집계 → 보고서.
def preprocess(inputs: list[Path], out_dir: Path, report_dir: Path | None = None) -> dict:
    # 여러 원본에서 읽은 행과 해당 파일의 인코딩을 추적할 수 있게 따로 보관한다.
    all_rows: list[tuple[str, dict[str, str]]] = []
    encodings: dict[str, str] = {}
    for path in inputs:
        rows, enc = read_source(path)
        encodings[str(path)] = enc
        all_rows.extend((str(path), row) for row in rows)

    invalid: list[dict] = []
    valid: list[tuple[date, str, str, str, Decimal]] = []
    invalid_reasons: Counter = Counter()
    missing_date_by_variety: Counter = Counter()
    missing_date_volume_by_variety: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    total_numeric_volume = Decimal(0)
    unassigned_numeric_volume = Decimal(0)
    unassigned_positive = 0
    seen_keys: dict[tuple, str] = {}
    collisions: list[str] = []

    # 원본 누락을 임의로 채우지 않고 제외 사유와 물량을 별도로 누적한다.
    for filename, row in all_rows:
        category, variety, office = row["품종대분류"], row["품종소분류"], row["신고소"]
        if not category or not variety or not office:
            reason = "missing_category_variety_or_office"
            invalid_reasons[reason] += 1
            invalid.append({"exclusion_reason": reason, **row})
            continue
        value = parse_tons(row["출하량"])
        if value is None:
            reason = "invalid_or_negative_volume"
            invalid_reasons[reason] += 1
            invalid.append({"exclusion_reason": reason, **row})
            continue
        total_numeric_volume += value

        date_text = row["출하일자"]
        # 날짜 없는 한라봉 기록은 어느 월에 해당하는지 알 수 없어 절대 월별로 배분하지 않는다.
        if not date_text:
            reason = "missing_date"
            invalid_reasons[reason] += 1
            missing_date_by_variety[variety] += 1
            missing_date_volume_by_variety[variety] += value
            unassigned_numeric_volume += value
            if value > 0:
                unassigned_positive += 1
            invalid.append({"exclusion_reason": reason, **row})
            continue
        try:
            day = date.fromisoformat(date_text)
            # YYYY-MM-DD 형식만 허용 (날짜 자료와 임의 포맷 혼합 금지)
            if day.isoformat() != date_text:
                raise ValueError("non-ISO format")
        except ValueError:
            reason = "invalid_date"
            invalid_reasons[reason] += 1
            unassigned_numeric_volume += value
            invalid.append({"exclusion_reason": reason, **row})
            continue

        # 일자·품종·신고소가 중복되면 두 번 합산할 위험이 있어 나중에 일괄 중단한다.
        key = (day, category, variety, office)
        if key in seen_keys:
            if len(collisions) < 10:
                collisions.append(f"{day} | {category} | {variety} | {office} | {seen_keys[key]} & {filename}")
        else:
            seen_keys[key] = filename
        valid.append((day, category, variety, office, value))

    # 특정 날짜+품종+신고소가 두 번 잡히면 합계가 중복계상될 수 있으므로 중단
    # 자동 중복 제거는 실제 서로 다른 출하를 잃게 할 수 있어 수동 확인을 요청한다.
    if collisions:
        raise ValueError(
            "동일 날짜·품종·신고소의 중복 키가 발견됐습니다. 임의 합산/제거하지 않고 중단합니다.\n"
            + "\n".join(collisions)
        )
    if not valid:
        raise ValueError("유효한 날짜가 있는 거래가 없습니다. 입력 데이터를 확인하세요.")

    date_min = min(row[0] for row in valid)
    date_max = max(row[0] for row in valid)
    last_month = date_max.strftime("%Y-%m")
    # 자료 종료일이 월말 이전이라면 마지막 달의 총량이 완전하지 않음을 표시한다.
    partial_last_month = date_max.day < calendar.monthrange(date_max.year, date_max.month)[1]

    daily_data: dict[tuple, dict] = {}
    monthly_data: dict[tuple, dict] = {}

    # 신고소를 합쳐 날짜×품종, 월×품종 단위로 집계하되 원본 단위(톤)를 유지한다.
    for day, category, variety, office, volume in valid:
        d_key = (day.isoformat(), category, variety)
        m_key = (day.strftime("%Y-%m"), category, variety)
        if d_key not in daily_data:
            daily_data[d_key] = {"tons": Decimal(0), "offices": set(), "positive_offices": set(), "rows": 0}
        if m_key not in monthly_data:
            monthly_data[m_key] = {"tons": Decimal(0), "days": set(), "rows": 0}
        daily_data[d_key]["tons"] += volume
        daily_data[d_key]["offices"].add(office)
        if volume > 0:
            daily_data[d_key]["positive_offices"].add(office)
        daily_data[d_key]["rows"] += 1
        monthly_data[m_key]["tons"] += volume
        monthly_data[m_key]["days"].add(day.isoformat())
        monthly_data[m_key]["rows"] += 1

    # 특정 품종의 날짜 미배정 기록 또는 마지막 부분월이면 데이터 품질 경고를 부여한다.
    def warnings(ym: str, variety: str) -> str:
        flags = []
        if missing_date_by_variety[variety]:
            flags.append("unknown_date_rows_excluded_for_variety")
        if partial_last_month and ym == last_month:
            flags.append("source_ends_mid_month")
        return "|".join(flags) if flags else "ok"

    # 산출 파일에서 신고소 수와 실제 양의 출하량 신고소 수를 별도로 제공한다.
    daily_rows = []
    for (day_str, category, variety), record in sorted(daily_data.items()):
        daily_rows.append({
            "date": day_str, "category": category, "variety": variety,
            "shipment_tons": ton_str(record["tons"]),
            "reporting_offices": len(record["offices"]),
            "positive_offices": len(record["positive_offices"]),
            "source_rows": record["rows"],
            "data_warning": warnings(day_str[:7], variety),
        })

    # observed_days는 달력의 전체 날짜 수가 아니라 원본에 기록된 날짜의 개수다.
    monthly_rows = []
    for (ym, category, variety), record in sorted(monthly_data.items()):
        monthly_rows.append({
            "year_month": ym, "category": category, "variety": variety,
            "shipment_tons": ton_str(record["tons"]),
            "observed_days": len(record["days"]),
            "source_rows": record["rows"],
            "data_warning": warnings(ym, variety),
        })

    valid_volume = sum((row[4] for row in valid), Decimal(0))
    daily_volume = sum((rec["tons"] for rec in daily_data.values()), Decimal(0))
    monthly_volume = sum((rec["tons"] for rec in monthly_data.values()), Decimal(0))
    # 원본 유효 행과 일·월간 합산값이 정확히 일치하는지 마지막에 검산한다.
    if not (valid_volume == daily_volume == monthly_volume):
        raise AssertionError("일별·월별 출하량 합계가 원본 유효 기록과 일치하지 않습니다.")

    # 날짜 미배정 물량을 별도로 보고하므로 누락 자료가 총합에서 사라진 것처럼 숨기지 않는다.
    report = {
        "source": "제주데이터허브 #855 신고소별 감귤류 및 만감류 출하량 통계",
        "source_files": encodings,
        "unit": "ton",
        "raw_rows": len(all_rows),
        "valid_dated_rows": len(valid),
        "excluded_rows": len(invalid),
        "excluded_by_reason": dict(sorted(invalid_reasons.items())),
        "missing_date_rows_with_positive_volume": unassigned_positive,
        "missing_date_rows_by_variety": dict(sorted(missing_date_by_variety.items())),
        "missing_date_volume_tons_by_variety": {
            k: ton_str(v) for k, v in sorted(missing_date_volume_by_variety.items())
        },
        "unassigned_numeric_volume_tons": ton_str(unassigned_numeric_volume),
        "total_numeric_source_volume_tons": ton_str(total_numeric_volume),
        "valid_dated_volume_tons": ton_str(valid_volume),
        "daily_volume_tons": ton_str(daily_volume),
        "monthly_volume_tons": ton_str(monthly_volume),
        "source_date_min": date_min.isoformat(),
        "source_date_max": date_max.isoformat(),
        "source_last_month_partial": partial_last_month,
        "distinct_source_dates": len({r[0] for r in valid}),
        "distinct_varieties": sorted({r[2] for r in valid}),
        "distinct_offices": sorted({r[3] for r in valid}),
        "daily_rows": len(daily_rows),
        "monthly_rows": len(monthly_rows),
        "integrity": {
            "valid_date_variety_office_keys_unique": True,
            "daily_and_monthly_totals_match": True,
            "missing_date_records_not_assigned_to_months": True,
            "missing_date_records_not_deduplicated": True,
            "unobserved_dates_not_filled_with_zero": True,
        },
        "interpretation_warnings": [
            "2025년 9월은 17일까지의 자료이므로 완전한 월간 통계가 아닙니다.",
            "한라봉에는 연월을 특정할 수 없는 날짜 누락 기록이 있습니다. 월별 추정이나 보간을 하지 않았습니다.",
            "신고 출하량(톤)은 경매시장 반입량과 다른 개념입니다. 원본 품종명 역시 경매 API 품종명과 직접 대응하지 않습니다.",
            "원본에서 나타나지 않는 품종·날짜를 0톤으로 보간하지 않았습니다.",
        ],
    }

    # 집계 CSV와 미배정 행·품질 보고서는 별도 위치에 보관한다.
    if report_dir is None:
        report_dir = out_dir.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    save_csv(out_dir / "daily_shipments.csv", DAILY_COLS, daily_rows)
    save_csv(out_dir / "monthly_shipments.csv", MONTHLY_COLS, monthly_rows)
    save_csv(report_dir / "shipment_unassigned_rows.csv", UNASSIGNED_COLS, invalid)
    (report_dir / "shipment_quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


# 사용자가 제공한 원본 경로를 읽고 처리 결과와 주의사항을 CMD에 출력한다.
def main() -> int:
    parser = argparse.ArgumentParser(description="제주데이터허브 #855 출하량 전처리")
    parser.add_argument(
        "--input", nargs="+", default=["data/raw/jeju_shipments.csv"], metavar="CSV",
        help="원본 CSV 경로 (여러 파일 지정 가능). 기본값: data/raw/jeju_shipments.csv",
    )
    parser.add_argument(
        "--output-dir", default="data/processed", metavar="DIR",
        help="출력 폴더. 기본값: data/processed",
    )
    parser.add_argument(
        "--report-dir", default="data/reports", metavar="DIR",
        help="검증 보고서·날짜 누락 기록 저장 폴더. 기본값: data/reports",
    )
    args = parser.parse_args()
    try:
        report = preprocess([Path(p) for p in args.input], Path(args.output_dir), Path(args.report_dir))
    except (OSError, ValueError, AssertionError) as exc:
        print(f"[전처리 실패] {exc}", file=sys.stderr)
        return 1
    print(f"[완료] 원본 {report['raw_rows']:,}건 → 정상 날짜 {report['valid_dated_rows']:,}건")
    print(f"[완료] 일별 {report['daily_rows']:,}행, 월별 {report['monthly_rows']:,}행")
    print(f"[주의] 날짜 미상 {report['excluded_by_reason'].get('missing_date', 0):,}건 / "
          f"{report['unassigned_numeric_volume_tons']}톤 (월별 집계에서 제외)")
    print(f"[저장] 가격·출하량 집계: {Path(args.output_dir).resolve()}")
    print(f"[저장] 검증 보고서: {Path(args.report_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
