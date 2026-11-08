"""Deterministic, read-only evidence tools for the personally implemented agent."""
from __future__ import annotations

import calendar
import csv
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median

from .config import PROJECT_ROOT


class DataError(ValueError):
    """Invalid source data rather than a user's missing condition."""


def _number(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("유한한 숫자가 필요합니다.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("유한한 숫자가 필요합니다.") from exc
    if not result.is_finite():
        raise ValueError("유한한 숫자가 필요합니다.")
    return result


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    return result


def _rows(path: Path, required: set[str]) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not required.issubset(reader.fieldnames or []):
            raise DataError("필수 데이터 컬럼이 없습니다.")
        if len(reader.fieldnames or []) != len(set(reader.fieldnames or [])):
            raise DataError("중복 컬럼이 있습니다.")
        rows = list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        raise DataError("CSV 컬럼 수가 맞지 않습니다.")
    return rows


def _error(status: str, message: str, **extra) -> dict:
    return {"status": status, "message": message, **extra}


def _shipment_rows(root: Path) -> list[dict]:
    rows = _rows(root / "data/processed/monthly_shipments.csv", {
        "year_month", "category", "variety", "shipment_tons", "observed_days", "data_warning",
    })
    seen = set()
    for row in rows:
        month = _day(row["year_month"] + "-01")
        if not row["category"].strip() or not row["variety"].strip():
            raise DataError("출하 분류가 없습니다.")
        volume = _number(row["shipment_tons"])
        days = int(row["observed_days"])
        if str(days) != row["observed_days"] or not 1 <= days <= calendar.monthrange(month.year, month.month)[1] or volume < 0:
            raise DataError("출하량 또는 관측일 수가 잘못됐습니다.")
        key = (row["year_month"], row["category"], row["variety"])
        if key in seen:
            raise DataError("중복 월별 출하량이 있습니다.")
        seen.add(key)
    return rows


def _shipment_source(root: Path, rows: list[dict]) -> tuple[dict, list[str], dict]:
    source = {"dataset": "제주데이터허브 #855 신고소별 감귤류 및 만감류 출하량 통계",
              "url": "https://www.jejudatahub.net/data/view/data/855",
              "file": "data/processed/monthly_shipments.csv", "unit": "ton",
              "source_first_date": None, "source_last_date": None}
    report_path = root / "data/reports/shipment_quality_report.json"
    report = {}
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        if not isinstance(report, dict):
            raise DataError("출하 품질보고서 형식이 잘못됐습니다.")
        if not isinstance(report.get("missing_date_rows_by_variety", {}), dict):
            raise DataError("날짜 누락 품종별 집계가 잘못됐습니다.")
    daily = root / "data/processed/daily_shipments.csv"
    if daily.exists():
        dates = [_day(r["date"]) for r in _rows(daily, {"date"})]
        if dates:
            source.update(source_first_date=min(dates).isoformat(), source_last_date=max(dates).isoformat())
    elif report.get("source_date_min") and report.get("source_date_max"):
        first, last = _day(report["source_date_min"]), _day(report["source_date_max"])
        if first > last:
            raise DataError("출하 관측기간이 잘못됐습니다.")
        source.update(source_first_date=first.isoformat(), source_last_date=last.isoformat())
    months = sorted({r["year_month"] for r in rows})
    if months:
        source.update(source_first_month=months[0], source_last_month=months[-1])
        if source["source_first_date"] and (source["source_first_date"][:7] != months[0] or source["source_last_date"][:7] != months[-1]):
            raise DataError("월별 파일과 관측기간이 일치하지 않습니다.")
    warnings = ["과거 신고 출하량이며 현재 실시간 수급 또는 미래가격 예측이 아닙니다.",
                "경매 품종명과 출하 품종명은 자동 매핑하지 않습니다. 출하량은 경매시장 반입량과 다릅니다."]
    if source["source_last_date"] is None:
        warnings.append("월간 파일만으로 정확한 일자 범위를 확인할 수 없습니다.")
    return source, warnings, report


def get_historical_shipment_context(args: dict, root: Path = PROJECT_ROOT) -> dict:
    """Return historical monthly values without imputing missing periods."""
    allowed = {"shipment_variety", "target_month", "start_year", "end_year"}
    try:
        if not isinstance(args, dict) or set(args) - allowed:
            raise ValueError("허용된 출하 조회조건을 입력해 주세요.")
        variety = args.get("shipment_variety")
        month = args.get("target_month")
        if not isinstance(variety, str) or not variety.strip():
            raise ValueError("출하자료의 품종명을 입력해 주세요.")
        variety = variety.strip()
        if type(month) is not int or not 1 <= month <= 12:
            raise ValueError("조회 월은 1~12의 정수여야 합니다.")
        first, last = args.get("start_year"), args.get("end_year")
        if any(v is not None and (type(v) is not int or not 1 <= v <= 9999) for v in (first, last)):
            raise ValueError("연도는 1~9999의 정수여야 합니다.")
        if first is not None and last is not None and first > last:
            raise ValueError("시작 연도가 종료 연도보다 늦습니다.")
    except ValueError as exc:
        return _error("INVALID_INPUT", str(exc))
    try:
        root = Path(root)
        rows = _shipment_rows(root)
        source, warnings, report = _shipment_source(root, rows)
        selected = [r for r in rows if r["variety"] == variety and int(r["year_month"][5:]) == month
                    and (first is None or int(r["year_month"][:4]) >= first)
                    and (last is None or int(r["year_month"][:4]) <= last)]
        common = {"input": args, "source": source, "warnings": warnings,
                  "is_future_forecast": False, "is_live_supply": False}
        if not selected:
            return _error("NO_DATA", "지정한 품종·기간의 관측자료가 없습니다.",
                          series=[], years_covered=[], available_varieties=sorted({r["variety"] for r in rows}), **common)
        if len({r["category"] for r in selected}) != 1:
            raise DataError("같은 품종명에 여러 출하 분류가 있습니다.")
        series = []
        for row in sorted(selected, key=lambda r: r["year_month"]):
            row_warning = [] if row["data_warning"].lower() == "ok" else [row["data_warning"]]
            end = source["source_last_date"]
            begin = source["source_first_date"]
            if begin and begin[:7] == row["year_month"] and _day(begin).day != 1:
                row_warning.append("PARTIAL_MONTH")
            if end and end[:7] == row["year_month"] and _day(end).day < calendar.monthrange(int(end[:4]), month)[1]:
                row_warning.append("PARTIAL_MONTH")
            if report.get("missing_date_rows_by_variety", {}).get(variety, 0):
                row_warning.append("MISSING_DATE_ROWS_EXCLUDED")
            series.append({"year": int(row["year_month"][:4]), "shipment_tons": str(_number(row["shipment_tons"])),
                           "observed_days": int(row["observed_days"]), "warning": sorted(set(row_warning))})
        if any("PARTIAL_MONTH" in r["warning"] for r in series):
            warnings.append("부분월은 완전한 월과 직접 비교할 수 없습니다.")
        complete = [r for r in series if "PARTIAL_MONTH" not in r["warning"]]
        values = [_number(r["shipment_tons"]) for r in complete]
        summary = {"min": str(min(values)) if values else None, "max": str(max(values)) if values else None,
                   "median": str(median(values)) if values else None, "years_count": len(values),
                   "excludes_partial_months": True}
        return {"status": "OK", "series": series, "years_covered": [r["year"] for r in series],
                "summary": summary, **common}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return _error("DATA_ERROR", "출하 데이터 파일과 품질보고서를 확인해 주세요.", error_kind=type(exc).__name__)


MARKET_FIELDS = {"market_code", "variety", "grade", "size", "package", "package_kg"}


def _market_rows(root: Path) -> list[dict]:
    rows = _rows(root / "data/processed/daily_prices.csv", MARKET_FIELDS | {
        "date", "variety_code", "grade_code", "size_code", "trade_type", "total_kg",
        "total_amount_krw", "transaction_count", "quality_flag",
    })
    seen = set()
    for row in rows:
        _day(row["date"])
        if any(not row[k].strip() for k in MARKET_FIELDS | {"variety_code", "grade_code", "size_code", "trade_type"}):
            raise DataError("가격 데이터 규격이 비어 있습니다.")
        if any(_number(row[k]) <= 0 for k in ("package_kg", "total_kg", "total_amount_krw")):
            raise DataError("가격 또는 중량이 양수가 아닙니다.")
        count = int(row["transaction_count"])
        if str(count) != row["transaction_count"] or count <= 0:
            raise DataError("거래 건수가 잘못됐습니다.")
        key = (row["date"], row["market_code"], row["variety_code"], row["grade_code"], row["size_code"],
               row["package"], _number(row["package_kg"]), row["trade_type"])
        if key in seen:
            raise DataError("같은 조건의 가격행이 중복됐습니다.")
        seen.add(key)
    return rows


def get_market_evidence(args: dict, root: Path = PROJECT_ROOT) -> dict:
    """Query an exact condition and recompute weighted price from source totals."""
    try:
        if not isinstance(args, dict) or set(args) - (MARKET_FIELDS | {"as_of"}):
            raise ValueError("허용된 시장 조회조건을 입력해 주세요.")
        missing = sorted(k for k in MARKET_FIELDS if args.get(k) is None or args.get(k) == "")
        if missing:
            return _error("NEEDS_INPUT", "시장가격 조회에 필요한 거래조건을 확인해 주세요.", missing_fields=missing)
        conditions = {}
        for key in MARKET_FIELDS - {"package_kg"}:
            if not isinstance(args[key], str) or not args[key].strip():
                raise ValueError("시장·품종·등급·크기·포장 조건은 문자열이어야 합니다.")
            conditions[key] = args[key].strip()
        conditions["package_kg"] = _number(args["package_kg"])
        if conditions["package_kg"] <= 0:
            raise ValueError("포장중량은 양수여야 합니다.")
        as_of = _day(args["as_of"]) if args.get("as_of") is not None else None
    except ValueError as exc:
        return _error("INVALID_INPUT", str(exc))
    try:
        from .data_refresh import resolve_market_root
        selected_root = resolve_market_root(Path(root))
        rows = _market_rows(selected_root)
        auction = [r for r in rows if r["trade_type"] == "경매"]
        dates = [_day(r["date"]) for r in auction]
        first, last = (min(dates), max(dates)) if dates else (None, None)
        source = {"dataset": "전국 공영도매시장 경매원천정보 API 수집본", "url": "https://www.data.go.kr/data/15141810/openapi.do",
                  "file": "data/processed/daily_prices.csv", "unit": "원/kg",
                  "source_first_date": first.isoformat() if first else None,
                  "source_last_date": last.isoformat() if last else None}
        source["snapshot_id"] = selected_root.name if selected_root != Path(root) else "shared-baseline"
        common = {"source": source, "input": args, "is_future_forecast": False,
                  "is_producer_net_price": False, "is_live_price": False}
        if not dates or (as_of and not first <= as_of <= last):
            return _error("NO_DATA", "요청한 기준일은 확보한 경매가격 관측기간 밖입니다.", **common)
        candidates = [r for r in auction if all((_number(r[k]) if k == "package_kg" else r[k]) == v
                                                  for k, v in conditions.items())]
        identities = {(r["variety_code"], r["grade_code"], r["size_code"]) for r in candidates}
        if len(identities) > 1:
            return _error("INVALID_INPUT", "같은 표시명에 여러 원천 코드가 있어 합칠 수 없습니다.",
                          reason="AMBIGUOUS_CODES", **common)
        candidates = [r for r in candidates if as_of is None or _day(r["date"]) <= as_of]
        if not candidates:
            return _error("NO_DATA", "같은 거래조건과 기준일에 맞는 경매자료가 없습니다.", **common)
        observed = max(r["date"] for r in candidates)
        latest = [r for r in candidates if r["date"] == observed]
        weight = sum((_number(r["total_kg"]) for r in latest), Decimal(0))
        amount = sum((_number(r["total_amount_krw"]) for r in latest), Decimal(0))
        flags = sorted({flag for r in latest for flag in r["quality_flag"].split(";") if flag and flag.lower() != "ok"})
        warnings = ["과거 관측 경락가격은 농가 실수취가격이나 미래 예측값이 아닙니다."]
        if observed != (as_of.isoformat() if as_of else last.isoformat()):
            warnings.append("요청 기준일보다 앞선 해당 거래조건의 최근 관측값입니다. 실제 관측일을 확인해 주세요.")
        if flags:
            warnings.append("원천 품질 경고가 있는 참고자료입니다: " + ", ".join(flags))
        return {"status": "OK", "conditions": {k: str(v) for k, v in conditions.items()},
                "observed_date": observed, "as_of": as_of.isoformat() if as_of else None,
                "price_per_kg": str(amount / weight), "total_kg": str(weight), "total_amount_krw": str(amount),
                "transaction_count": sum(int(r["transaction_count"]) for r in latest),
                "quality_flags": flags, "warnings": warnings, **common}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return _error("DATA_ERROR", "가격 데이터 파일을 확인해 주세요.", error_kind=type(exc).__name__)
