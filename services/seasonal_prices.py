"""Same-spec seasonal auction prices and comparisons with a gross break-even.

This is an observation of different auction lots, NOT a future price forecast or
an estimate of what the same farmer's stored fruit would earn.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEASONS = ("2023-2024", "2024-2025", "2025-2026", "2026-2027")
REQUIRED = {"date", "market_code", "variety", "variety_code", "grade", "grade_code",
            "size", "size_code", "package", "package_kg", "trade_type",
            "total_kg", "total_amount_krw", "transaction_count", "quality_flag"}
FILTERS = ("market_code", "variety", "grade", "size", "package", "package_kg", "trade_type")


def _number(value):
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise ValueError("숫자 입력을 확인해 주세요.") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("포장중량은 양수여야 합니다.")
    return number


@lru_cache(maxsize=12)
def _load_csv(path: str, stat_size: int, stat_mtime_ns: int):
    # Size and mtime in cache key ensure a new local season version is reloaded.
    del stat_size, stat_mtime_ns
    rows = []
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames):
            raise ValueError("시즌 가격 CSV의 열 구성이 다릅니다.")
        for row in reader:
            if any(row[key] is None for key in REQUIRED):
                raise ValueError("시즌 가격 CSV의 일부 항목이 누락되었습니다.")
            observed = date.fromisoformat(row["date"])
            weight = _number(row["total_kg"])
            amount = _number(row["total_amount_krw"])
            count = int(row["transaction_count"])
            if count <= 0:
                raise ValueError("거래 건수가 올바르지 않습니다.")
            rows.append((observed, row, weight, amount, count))
    return tuple(rows)


def _snapshot(path: Path):
    st = path.stat()
    return _load_csv(str(path), st.st_size, st.st_mtime_ns)


def season_price_file(root: Path, season: str) -> Path:
    return Path(root) / "data" / "seasons" / season / "daily_prices.csv"


def seasonal_price_revision(root: Path = PROJECT_ROOT):
    """A change to any season CSV invalidates the UI's previous price lookup."""
    signature = []
    for season in SEASONS:
        try:
            stat = season_price_file(root, season).stat()
            signature.append((season, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            signature.append((season, None, None))
        except OSError:
            signature.append((season, "DATA_ERROR", None))
    return signature


def _season_day(season: str, month_day: str):
    month, day = (int(v) for v in month_day.split("-"))
    start = int(season[:4])
    return date(start if month >= 10 else start + 1, month, day)


def compare_seasonal_prices(query: dict, root: Path = PROJECT_ROOT) -> dict:
    """Compare seasonal matched auction lots around an identical month/day.

    query: market_code, variety, grade, size, package, package_kg,
           trade_type, month_day (MM-DD), window_days (0..14).
    No input or model-produced price is ever written to stored comparison state.
    """
    summary, _ = _seasonal_evidence(query, root)
    return summary


def _seasonal_evidence(query: dict, root: Path):
    """Return the public summary and the exact same matched rows for the threshold check."""
    if not isinstance(query, dict):
        return {"status": "INVALID_INPUT", "message": "조회조건을 확인해 주세요."}, {}
    unknown = set(query) - set((*FILTERS, "month_day", "window_days"))
    if unknown:
        return {"status": "INVALID_INPUT", "message": "지원하지 않는 조회조건입니다."}, {}
    missing = [key for key in (*FILTERS, "month_day") if query.get(key) in (None, "")]
    if missing:
        return {"status": "NEEDS_INPUT", "missing_fields": missing,
                "message": "같은 규격과 비교할 월·일을 확인해 주세요."}, {}
    try:
        filters = {key: str(query[key]).strip() for key in FILTERS if key != "package_kg"}
        package_kg = _number(query["package_kg"])
        if not all(filters.values()):
            raise ValueError("거래규격이 비어 있습니다.")
        month_day = str(query["month_day"])
        if len(month_day) != 5 or month_day[2] != "-" or not (month_day[:2] + month_day[3:]).isdigit():
            raise ValueError("조회 월·일은 MM-DD 형식이어야 합니다.")
        month = int(month_day[:2])
        if not (month >= 10 or month <= 2):
            raise ValueError("현재 확보된 시즌의 비교 가능 월은 10월~2월입니다.")
        window = query.get("window_days", 7)
        if type(window) is not int or not 0 <= window <= 14:
            raise ValueError("비교 범위는 전후 0~14일입니다.")
        # Feb 29 is possible only in leap seasons; do not silently replace it with Feb 28.
        date(2024 if month == 2 and int(month_day[3:]) == 29 else 2025, month, int(month_day[3:]))
    except (ValueError, TypeError) as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}, {}

    output = []
    matching_codes = set()
    matched_by_season = {}
    for season in SEASONS:
        path = season_price_file(root, season)
        item = {"season": season, "status": "NOT_PREPARED", "observed_days": 0,
                "transaction_count": 0, "total_kg": None, "price_per_kg": None,
                "source_first_date": None, "source_last_date": None,
                "quality_warnings": [], "window_complete": False}
        try:
            center = _season_day(season, month_day)
        except ValueError:
            center = None
        if center is not None:
            begin, end = center - timedelta(days=window), center + timedelta(days=window)
            item.update(window_start=begin.isoformat(), window_end=end.isoformat())
        try:
            rows = _snapshot(path)
            if not rows:
                item.update(status="NO_DATA")
                output.append(item)
                continue
            dates = [x[0] for x in rows]
            first, last = min(dates), max(dates)
            item.update(source_first_date=first.isoformat(), source_last_date=last.isoformat())
            if center is None:
                item.update(status="INVALID_CALENDAR_DATE")
                output.append(item)
                continue
            item.update(window_complete=(first <= begin and end <= last))
            matched = [(observed, row, weight, amount, count)
                       for observed, row, weight, amount, count in rows
                       if begin <= observed <= end
                       and all(row[key] == filters[key] for key in filters)
                       and _number(row["package_kg"]) == package_kg]
            if not matched:
                item.update(status="NO_DATA")
                output.append(item)
                continue
            codes = {(row["variety_code"], row["grade_code"], row["size_code"])
                     for _, row, *_ in matched}
            if len(codes) != 1:
                item.update(status="AMBIGUOUS_CODES")
                output.append(item)
                continue
            matching_codes.update(codes)
            total_kg = sum((weight for _, _, weight, _, _ in matched), Decimal(0))
            total_amount = sum((amount for _, _, _, amount, _ in matched), Decimal(0))
            warnings = sorted({flag for _, row, *_ in matched
                               for flag in row["quality_flag"].split(";")
                               if flag and flag.upper() != "OK"})
            item.update(status="OK", observed_days=len({observed for observed, *_ in matched}),
                         transaction_count=sum(count for *_, count in matched),
                        total_kg=str(total_kg),
                        price_per_kg=str((total_amount / total_kg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                        quality_warnings=warnings,
                         classification_codes=list(next(iter(codes))))
            matched_by_season[season] = matched
        except FileNotFoundError:
            pass
        except (OSError, ValueError, OverflowError, csv.Error):
            item.update(status="DATA_ERROR")
        output.append(item)
    if len(matching_codes) > 1:
        return {"status": "AMBIGUOUS_CODES", "message": "시즌마다 같은 표시명의 원천 품종·등급·크기 코드가 다릅니다. 자동 합산하지 않습니다.",
                "seasons": output}, {}
    observed = [x for x in output if x["status"] == "OK"]
    source_errors = any(x["status"] == "DATA_ERROR" for x in output)
    inspected = any(x["status"] in {"OK", "NO_DATA", "AMBIGUOUS_CODES"} for x in output)
    status = "NO_DATA" if not observed else ("OK" if len(observed) == len(SEASONS) else "PARTIAL_DATA")
    if source_errors and not inspected:
        status = "DATA_ERROR"
    summary = {"status": status, "analysis_type": "seasonal_prices",
            "conditions": {**filters, "package_kg": str(package_kg), "month_day": month_day, "window_days": window},
            "seasons": output,
            "observed_seasons": len(observed),
            "source_partial": source_errors and inspected,
            "source": {"dataset": "전국 공영도매시장 경매원천정보 · 제주산 감귤 시즌별 집계",
                       "url": "https://www.data.go.kr/data/15141810/openapi.do", "unit": "원/kg",
                       "basis": "동일 등록 규격 · 거래중량 가중 경락가격"},
            "warnings": ["시즌별 과거 거래가격입니다. 미래가격·농가 실수취액·보관 효과를 예측하지 않습니다.",
                         "창의 양 끝이 원천 관측기간 밖이면 부분 구간으로 표시합니다."]}
    if status == "DATA_ERROR":
        summary["message"] = "시즌 가격자료를 읽지 못했습니다. 자료 상태를 확인한 뒤 다시 조회해 주세요."
    return summary, matched_by_season


def _pct(num: Decimal, denom: Decimal) -> str | None:
    return str((num / denom * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) if denom else None


def compare_break_even_to_history(query: dict, threshold, *, price_basis: str,
                                  root: Path = PROJECT_ROOT) -> dict:
    """Compare past daily same-spec averages with a gross break-even in nominal KRW."""
    if price_basis != "gross":
        return {"status": "NOT_COMPARABLE", "message":
                "손익분기 단가가 수수료 차감 후 기준입니다. 수수료 차감 전 경락가격과 직접 비교하지 않습니다."}
    try:
        t = Decimal(str(threshold))
        if not t.is_finite() or t <= 0:
            raise ValueError("손익분기 단가가 올바르지 않습니다.")
    except (ValueError, TypeError, InvalidOperation) as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}
    basis, matched = _seasonal_evidence(query, root)
    if basis["status"] in {"INVALID_INPUT", "NEEDS_INPUT", "AMBIGUOUS_CODES"}:
        return basis
    output = []
    for item in basis["seasons"]:
        season = item["season"]
        new = {"season": season, "source_status": item["status"],
               "source_first_date": item.get("source_first_date"),
               "source_last_date": item.get("source_last_date"),
               "window_start": item.get("window_start"),
               "window_end": item.get("window_end"),
               "window_complete": item.get("window_complete", False),
               "days_matching": 0, "days_clean": 0, "days_above": 0,
               "clean_kg": None, "kg_on_days_above": None,
               "daily_share_percent": None, "kg_share_percent": None,
               "excluded_flagged_days": 0, "status": item["status"]}
        output.append(new)
        if item["status"] != "OK":
            continue
        days = defaultdict(lambda: {"kg": Decimal(0), "amount": Decimal(0), "flagged": False})
        for observed, row, kg, amount, _ in matched[season]:
            day = days[observed]
            day["kg"] += kg
            day["amount"] += amount
            if any(flag.strip() and flag.strip().upper() != "OK"
                   for flag in row["quality_flag"].split(";")):
                day["flagged"] = True
        usable = [v for v in days.values() if not v["flagged"]]
        flagged = sum(v["flagged"] for v in days.values())
        new.update(days_matching=len(days), excluded_flagged_days=flagged)
        if not usable:
            new["status"] = "NO_CLEAN_DAYS"
            continue
        above = [v for v in usable if v["amount"] / v["kg"] >= t]
        clean_kg = sum((v["kg"] for v in usable), Decimal(0))
        above_kg = sum((v["kg"] for v in above), Decimal(0))
        new.update(status="OK", days_clean=len(usable), days_above=len(above),
                   clean_kg=str(clean_kg), kg_on_days_above=str(above_kg),
                   daily_share_percent=_pct(Decimal(len(above)), Decimal(len(usable))),
                   kg_share_percent=_pct(above_kg, clean_kg))
    status = basis["status"]
    if status in ("OK", "PARTIAL_DATA"):
        status = "OK" if any(s["status"] == "OK" for s in output) else "NO_CLEAN_DAYS"
    return {"status": status,
            "analysis_type": "seasonal_threshold",
            "threshold_krw_per_kg": str(t), "price_basis": "gross",
            "conditions": basis["conditions"], "seasons": output, "source": basis["source"],
            "source_partial": basis["source_partial"],
            **({"message": basis["message"]} if "message" in basis else {}),
            "warnings": [
                "기준가격 이상 비율은 해당 규격의 과거 *일별 평균 경락가격*으로 계산했습니다. 개별 거래가격의 비율이 아닙니다.",
                "과거 관측 비율은 미래 가격 도달 확률이나 특정 농가의 실수취 가능성이 아닙니다.",
                "원천 품질 경고가 있는 날짜는 비율 계산에서 제외했습니다. 연도별 물량·품질·물가와 관측일이 다릅니다.",
                "가락시장 경락가격과 농가 제시가격은 가격 형성 및 공제 항목이 다를 수 있습니다.",
            ]}
