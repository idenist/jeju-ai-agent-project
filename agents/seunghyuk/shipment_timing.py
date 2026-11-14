"""제주데이터허브 #855의 과거 출하량으로 두 출하 예정일 주변을 비교합니다.

- 경매 품종명과 출하통계 품종명을 자동 연결하지 않습니다.
- 두 날짜 주변 각 7일간의 *관측된* 신고 출하량(톤)을 연도별로 비교합니다.
- 누락된 날을 0으로 채우지 않고, 두 구간 모두 7일 관측된 연도만 요약합니다.
- 현재 출하량·미래 가격·유리한 출하일을 예측하지 않습니다.
- Python 표준 라이브러리만 사용, 데이터 변경 없음.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
SOURCE_NAME = "제주데이터허브 #855 신고소별 감귤류 및 만감류 출하량 통계"
SOURCE_URL = "https://www.jejudatahub.net/data/view/data/855"
DAILY_PATH = Path("data/processed/daily_shipments.csv")
REPORT_PATH = Path("data/reports/shipment_quality_report.json")
REFERENCE_YEARS = tuple(range(2018, 2025))
WINDOW_DAYS = 7
RADIUS = 3
# 원본의 분류를 사용합니다. 거래소의 조생귤 등과 동일하다고 가정하지 않습니다.
CATEGORIES = {
    "노지온주": "감귤류", "월동온주": "감귤류", "하우스온주": "감귤류",
    "한라봉": "만감류", "천혜향": "만감류", "레드향": "만감류",
}


class DataIssue(ValueError):
    pass


def _response(status: str, message: str, **extra) -> dict:
    return {"status": status, "analysis_type": "shipment_timing", "message": message,
            "is_future_forecast": False, "is_live_supply": False,
            "price_effect_estimated": False, "recommended_option": None, **extra}


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    parsed = date.fromisoformat(value)
    if value != parsed.isoformat():
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    return parsed


def _decimal(value: object) -> Decimal:
    try:
        out = Decimal(str(value))
        if not out.is_finite() or out < 0:
            raise ValueError("출하량은 0 이상의 유한한 숫자여야 합니다.")
        return out
    except (InvalidOperation, TypeError) as exc:
        raise DataIssue("출하량 숫자가 손상되었습니다.") from exc


def _two(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _load(root: Path) -> tuple[dict, dict, str]:
    path = root / DAILY_PATH
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    lines = raw.decode("utf-8-sig").splitlines()
    reader = csv.DictReader(lines)
    required = {"date", "category", "variety", "shipment_tons", "data_warning"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames) or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise DataIssue("출하량 CSV 컬럼 구성이 잘못됐습니다.")
    data: dict[tuple[date, str, str], tuple[Decimal, str]] = {}
    for idx, row in enumerate(reader, start=2):
        if None in row or any(v is None for v in row.values()):
            raise DataIssue(f"출하량 CSV {idx}행 컬럼 수가 잘못됐습니다.")
        try:
            day = _parse_date(row["date"])
            amount = _decimal(row["shipment_tons"])
        except ValueError as exc:
            raise DataIssue(f"출하량 CSV {idx}행: {exc}") from exc
        category, variety = row["category"].strip(), row["variety"].strip()
        if not category or not variety:
            raise DataIssue(f"출하량 CSV {idx}행: 분류가 비어 있습니다.")
        if variety in CATEGORIES and category != CATEGORIES[variety]:
            raise DataIssue(f"출하량 CSV {idx}행: 분류와 품종이 다릅니다.")
        key = (day, category, variety)
        if key in data:
            raise DataIssue(f"중복 출하량 집계행: {key}")
        data[key] = (amount, row["data_warning"])
    if not data:
        raise DataIssue("출하량 CSV가 비어 있습니다.")
    report_path = root / REPORT_PATH
    quality = json.loads(report_path.read_text(encoding="utf-8-sig")) if report_path.exists() else {}
    if quality and (not isinstance(quality, dict) or
                    quality.get("daily_rows") != len(data) or
                    quality.get("unit") != "ton"):
        raise DataIssue("출하량 전처리 품질보고서와 실제 CSV가 일치하지 않습니다.")
    return data, quality, sha


def _reference_date(day: date, year: int, base_year: int) -> date:
    # 12월→다음해 1월 비교 시 다른 연도의 실제 자료를 연결합니다.
    return date(year + day.year - base_year, day.month, day.day)


def _window(data: dict, center: date, variety: str) -> tuple[Decimal | None, int, list[str]]:
    amounts: list[Decimal] = []
    flags: set[str] = set()
    for offset in range(-RADIUS, RADIUS + 1):
        rec = data.get((center + timedelta(days=offset), CATEGORIES[variety], variety))
        if rec is not None:
            amounts.append(rec[0])
            if rec[1].strip().lower() != "ok":
                flags.update(x for x in rec[1].split("|") if x)
    return (sum(amounts, Decimal(0)) if len(amounts) == WINDOW_DAYS else None,
            len(amounts), sorted(flags))


def resolve_pair_dates(pair_inputs: dict | None) -> tuple[str | None, str | None]:
    """현재 미래 A/B 입력에서 출하 예정일만 가져옵니다. 비용·가정 단가는 사용하지 않습니다."""
    if not isinstance(pair_inputs, dict):
        return None, None
    try:
        start = _parse_date(pair_inputs.get("start_date"))
        options = (pair_inputs["option_a"], pair_inputs["option_b"])
        days = [x["delay_days"] for x in options]
        if any(type(x) is not int or not 1 <= x <= 365 for x in days):
            return None, None
        a, b = (start + timedelta(days=x) for x in days)
        if a >= b:
            return None, None
        return a.isoformat(), b.isoformat()
    except (KeyError, ValueError, TypeError, OverflowError):
        return None, None


def analyze_shipment_timing(args: dict, root: Path = ROOT) -> dict:
    """Historical A/B dates, matched within each 2018–24 reference season.

    Returns a tool-friendly result with explicit coverage; no imputation or recommendation.
    """
    allowed = {"shipment_variety", "option_a_date", "option_b_date"}
    if not isinstance(args, dict) or set(args) - allowed:
        return _response("INVALID_INPUT", "출하 품종과 A/B 출하일만 입력해 주세요.")
    variety = args.get("shipment_variety")
    if not isinstance(variety, str) or not variety.strip():
        return _response("NEEDS_INPUT", "제주데이터허브 출하통계 품종을 먼저 선택해 주세요. 경매 품종과 자동 연결하지 않습니다.",
                         missing_fields=["shipment_variety"], available_varieties=list(CATEGORIES))
    variety = variety.strip()
    if variety not in CATEGORIES:
        return _response("INVALID_INPUT", "제주데이터허브 원본의 출하 품종 중 하나를 선택해 주세요.",
                         available_varieties=list(CATEGORIES))
    if not args.get("option_a_date") or not args.get("option_b_date"):
        return _response("NEEDS_INPUT", "비교할 A안·B안의 실제 출하 예정일이 필요합니다.",
                         missing_fields=[k for k in ("option_a_date", "option_b_date") if not args.get(k)],
                         available_varieties=list(CATEGORIES))
    try:
        a, b = (_parse_date(args[k]) for k in ("option_a_date", "option_b_date"))
        if not (a < b and (b - a).days <= 365):
            raise ValueError("A안은 B안보다 이르고, 두 날짜 간격은 1~365일이어야 합니다.")
    except (ValueError, TypeError, OverflowError) as exc:
        return _response("INVALID_INPUT", str(exc))
    try:
        data, report, sha = _load(Path(root))
        source_first = min(d for d, _, _ in data).isoformat()
        source_last = max(d for d, _, _ in data).isoformat()
        source = {
            "dataset": SOURCE_NAME, "url": SOURCE_URL, "file": str(DAILY_PATH),
            "unit": "ton", "source_first_date": source_first, "source_last_date": source_last,
            "data_sha256": sha, "reference_years_requested": list(REFERENCE_YEARS),
            "window_days": WINDOW_DAYS, "window_definition": "중심일 전후 3일씩 포함한 7일",
        }
        years, skipped = [], []
        for year in REFERENCE_YEARS:
            try:
                pa = _reference_date(a, year, a.year)
                pb = _reference_date(b, year, a.year)
            except ValueError:
                skipped.append({"reference_year": year, "reason": "해당 연도에 날짜가 없음(2월 29일 등)"})
                continue
            av, an, aflags = _window(data, pa, variety)
            bv, bn, bflags = _window(data, pb, variety)
            if av is None or bv is None:
                skipped.append({"reference_year": year, "observed_days_a": an, "observed_days_b": bn,
                                "reason": "A/B 중 7일 완전 관측이 아닌 구간 있음(결측을 0으로 취급하지 않음)"})
                continue
            difference = bv - av
            percent = ((bv / av - 1) * Decimal(100)) if av > 0 else None
            years.append({
                "reference_year": year, "historical_a_date": pa.isoformat(), "historical_b_date": pb.isoformat(),
                "a_tons": _two(av), "b_tons": _two(bv), "difference_b_minus_a_tons": _two(difference),
                "change_b_vs_a_pct": _two(percent) if percent is not None else None,
                "observed_days_a": an, "observed_days_b": bn, "data_warnings": sorted(set(aflags + bflags)),
            })
        warnings = [
            "실시간 공급량·미래 출하량·경락가격 예측이 아닌, 과거 같은 달력일 주변의 신고 출하량 비교입니다.",
            "제주데이터허브 신고 출하량은 도매시장 반입량과 다릅니다. 경매 품종과 자동 매핑하지 않습니다.",
            "과거 출하량 차이만으로 가격의 상승·하락이나 더 유리한 출하일을 판단할 수 없습니다.",
            "완전한 7일 관측치가 없는 연도는 분석 요약에서 제외합니다. 누락일을 0톤으로 보간하지 않습니다.",
        ]
        missing = (report.get("missing_date_rows_by_variety") or {}).get(variety, 0)
        if missing:
            missing_tons = (report.get("missing_date_volume_tons_by_variety") or {}).get(variety)
            warnings.append(f"{variety} 날짜 없는 원본 {missing:,}행({missing_tons}톤)이 날짜별 집계에서 제외됐습니다. 비교가 과소 집계될 수 있습니다.")
        if a.year >= 2025 or b.year >= 2025:
            warnings.append("2025년 9월 17일 이후 관측이 없으므로 2025~현재 출하량의 실제 변화는 판단할 수 없습니다.")
        common = {
            "input": {"shipment_variety": variety, "option_a_date": a.isoformat(), "option_b_date": b.isoformat()},
            "source": source, "years": years, "excluded_reference_years": skipped,
            "paired_year_count": len(years), "warnings": warnings,
            "method": "2018~2024 연도별 동일 월일의 전후 3일(총 7일) 출하량을 각각 합산한 후, A/B 모두 7일 완전 관측된 연도만 짝지어 비교",
        }
        if len(years) < 3:
            return _response("INSUFFICIENT_DATA", f"두 출하 시기를 공정하게 비교할 수 있는 연도가 {len(years)}개뿐입니다(최소 3개 필요).", **common)
        av = [Decimal(row["a_tons"]) for row in years]
        bv = [Decimal(row["b_tons"]) for row in years]
        diffs = [bval - aval for aval, bval in zip(av, bv)]
        pct = [Decimal(row["change_b_vs_a_pct"]) for row in years if row["change_b_vs_a_pct"] is not None]
        summary = {
            "a_median_7day_tons": _two(median(av)),
            "b_median_7day_tons": _two(median(bv)),
            "paired_median_b_minus_a_tons": _two(median(diffs)),
            "paired_median_change_pct": _two(median(pct)) if len(pct) >= 3 else None,
            "paired_change_pct_years": len(pct),
            "b_higher_years": sum(d > 0 for d in diffs),
            "a_higher_years": sum(d < 0 for d in diffs),
            "equal_years": sum(d == 0 for d in diffs),
        }
        if (b - a).days < WINDOW_DAYS:
            warnings.append("두 7일 관측창이 일부 겹칩니다. 완전히 독립된 출하량 구간으로 해석하지 않습니다.")
        return _response("OK", "과거 동일 시기 출하량의 A/B 비교를 완료했습니다. 미래 가격이나 유리한 출하일 추천은 하지 않습니다.",
                         summary=summary, **common)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError, OverflowError, ArithmeticError) as exc:
        return _response("DATA_ERROR", "출하량 CSV·품질보고서를 읽거나 검증하지 못했습니다.", error_kind=type(exc).__name__)


def format_timing_report(result: dict) -> str:
    """화면/Agent 보고서에 동일 문구와 표를 재사용합니다."""
    if not isinstance(result, dict):
        return "출하 시기 분석 결과의 형식이 올바르지 않습니다."
    status = result.get("status")
    if status not in {"OK", "INSUFFICIENT_DATA"}:
        variants = ", ".join(result.get("available_varieties", []))
        return "**출하 시기 분석** — " + result.get("message", status or "오류") + ("\n\n선택 가능한 출하 품종: " + variants if variants else "")
    lines = ["**제주데이터허브 과거 출하 시기 A/B 분석**", "",
             f"선택한 출하통계 품종: {result['input']['shipment_variety']} · "
             f"A안 {result['input']['option_a_date']} · B안 {result['input']['option_b_date']}",
             f"과거 {result['paired_year_count']}개 연도 비교 (기준: 2018~2024, 날짜 전후 3일 포함 7일)"]
    if status == "OK":
        s = result["summary"]
        lines += ["", f"- A 주변 7일 출하량 중앙값: **{Decimal(s['a_median_7day_tons']):,.2f}톤**",
                  f"- B 주변 7일 출하량 중앙값: **{Decimal(s['b_median_7day_tons']):,.2f}톤**",
                  f"- B가 더 많았던 해: **{s['b_higher_years']}/{result['paired_year_count']}개**",
                  f"- 같은 연도의 출하량 차이 중앙값(B안 - A안): **{Decimal(s['paired_median_b_minus_a_tons']):+,.2f}톤**"]
        if s["paired_median_change_pct"] is not None:
            lines.append(f"- 같은 연도 대비 증감률 중앙값: **{Decimal(s['paired_median_change_pct']):+,.2f}%**")
    else:
        lines += ["", f"**비교 보류:** {result['message']}"]
    lines += ["", "| 비교 연도 | A 주변 7일(톤) | B 주변 7일(톤) | B안 - A안(톤) |",
              "| --- | ---: | ---: | ---: |"]
    for row in result["years"]:
        lines.append(f"| {row['reference_year']} | {Decimal(row['a_tons']):,.2f} | "
                     f"{Decimal(row['b_tons']):,.2f} | {Decimal(row['difference_b_minus_a_tons']):+,.2f} |")
    if not result["years"]:
        lines.append("| 완전한 짝 관측 없음 | — | — | — |")
    lines += ["", f"출처: [{SOURCE_NAME}]({SOURCE_URL}) · 데이터 {result['source']['source_first_date']}~{result['source']['source_last_date']}",
              "\n".join(f"- 주의: {x}" for x in result.get("warnings", []))]
    return "\n".join(lines)
