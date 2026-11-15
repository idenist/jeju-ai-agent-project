"""Historical daily auction PRICE LEVEL versus a user-calculated gross break-even.

Not a transaction-level price success rate, not a forecast or a probability.
The source is a daily same-spec aggregation, not individual auction events.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from agents.common.seasonal_prices import (
    FILTERS, SEASONS, PROJECT_ROOT, _number, _season_day, _snapshot,
    compare_seasonal_prices,
)


def _pct(num: Decimal, denom: Decimal) -> str | None:
    return str((num / denom * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) if denom else None


def compare_break_even_to_history(query: dict, threshold, *, price_basis: str,
                                  root: Path = PROJECT_ROOT) -> dict:
    """Calculate how often historical *daily same-spec mean* met a gross threshold.

    Cross-season nominal KRW values are NOT inflation-adjusted. In particular,
    no claim is made about a particular lot's price or next week's prices.
    """
    if price_basis != "gross":
        return {"status": "NOT_COMPARABLE", "message":
                "손익분기 단가가 수수료 차감 후 기준입니다. 수수료 차감 전 경락가격과 직접 비교하지 않습니다."}
    try:
        t = Decimal(str(threshold))
        if not t.is_finite() or t <= 0:
            raise ValueError("손익분기 단가가 올바르지 않습니다.")
    except (ValueError, TypeError, InvalidOperation) as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}
    basis = compare_seasonal_prices(query, root=root)
    if basis["status"] in {"INVALID_INPUT", "NEEDS_INPUT", "AMBIGUOUS_CODES"}:
        return basis
    if basis["status"] == "NO_DATA":
        return {**basis, "status": "NO_DATA", "threshold_krw_per_kg": str(t)}

    conditions = basis["conditions"]
    try:
        filters = {k: str(conditions[k]) for k in FILTERS if k != "package_kg"}
        package_kg = _number(conditions["package_kg"])
    except (KeyError, ValueError, TypeError) as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}
    output = []
    for item in basis["seasons"]:
        season = item["season"]
        new = {"season": season, "source_status": item["status"],
               "source_first_date": item.get("source_first_date"),
               "source_last_date": item.get("source_last_date"),
               "window_complete": item.get("window_complete", False),
               "days_matching": 0, "days_clean": 0, "days_above": 0,
               "clean_kg": None, "kg_on_days_above": None,
               "daily_share_percent": None, "kg_share_percent": None,
               "excluded_flagged_days": 0, "status": item["status"]}
        if item["status"] != "OK":
            output.append(new)
            continue
        path = Path(root) / "data" / "seasons" / season / "daily_prices.csv"
        try:
            center = _season_day(season, conditions["month_day"])
            begin = center - timedelta(days=conditions["window_days"])
            end = center + timedelta(days=conditions["window_days"])
            days = defaultdict(lambda: {"kg": Decimal(0), "amount": Decimal(0), "flagged": False})
            for observed, row, kg, amount, count in _snapshot(path):
                if not begin <= observed <= end:
                    continue
                if not all(row[k] == filters[k] for k in filters):
                    continue
                if _number(row["package_kg"]) != package_kg:
                    continue
                day = days[observed]
                day["kg"] += kg
                day["amount"] += amount
                if any(flag.strip() and flag.strip().upper() != "OK"
                       for flag in row["quality_flag"].split(";")):
                    day["flagged"] = True
            usable = [v for v in days.values() if not v["flagged"]]
            flagged = sum(v["flagged"] for v in days.values())
            if not usable:
                new.update(status="NO_CLEAN_DAYS", days_matching=len(days), excluded_flagged_days=flagged)
                output.append(new)
                continue
            above = [v for v in usable if v["amount"] / v["kg"] >= t]
            clean_kg = sum((v["kg"] for v in usable), Decimal(0))
            above_kg = sum((v["kg"] for v in above), Decimal(0))
            new.update(status="OK", days_matching=len(days), days_clean=len(usable),
                       days_above=len(above), excluded_flagged_days=flagged,
                       clean_kg=str(clean_kg), kg_on_days_above=str(above_kg),
                       daily_share_percent=_pct(Decimal(len(above)), Decimal(len(usable))),
                       kg_share_percent=_pct(above_kg, clean_kg))
        except (OSError, ValueError, UnicodeError, OverflowError):
            new.update(status="DATA_ERROR")
        output.append(new)
    ok = [s for s in output if s["status"] == "OK"]
    return {"status": "OK" if ok else "NO_CLEAN_DAYS", "analysis_type": "seasonal_threshold",
            "threshold_krw_per_kg": str(t), "price_basis": "gross",
            "conditions": conditions, "seasons": output,
            "warnings": [
                "기준가격 이상 비율은 해당 규격의 과거 *일별 평균 경락가격*으로 계산했습니다. 개별 거래가격의 비율이 아닙니다.",
                "과거 관측 비율은 미래 가격 도달 확률이나 특정 농가의 실수취 가능성이 아닙니다.",
                "원천 품질 경고가 있는 날짜는 비율 계산에서 제외했습니다. 연도별 물량·품질·물가와 관측일이 다릅니다.",
                "가락시장 경락가격과 농가 제시가격은 가격 형성 및 공제 항목이 다를 수 있습니다.",
            ]}
