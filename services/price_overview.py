"""Read-only period summaries of archived auction data, with optional filters.

Broad market observations do not require a farmer's full trading specification.
Keep markets and variety codes separate; weight by traded kg, never by row count.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from services.seasonal_prices import PROJECT_ROOT, SEASONS, _snapshot, season_price_file

FILTERS = ("market", "variety", "grade", "size", "package", "package_kg")
SOURCE = {"dataset": "전국 공영도매시장 경매원천정보 · 제주산 감귤 시즌별 집계",
          "url": "https://www.data.go.kr/data/15141810/openapi.do", "unit": "원/kg"}


def _reply(status, **values):
    return {"analysis_type": "price_overview", "status": status, **values}


def _price(rows):
    weight = sum((r[2] for r in rows), Decimal(0))
    amount = sum((r[3] for r in rows), Decimal(0))
    return str((amount / weight).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def get_price_overview(query, *, root=PROJECT_ROOT):
    if not isinstance(query, dict) or set(query) - {*FILTERS, "start_date", "end_date"}:
        return _reply("INVALID_INPUT", message="조회 기간과 선택 조건을 확인해 주세요.")
    if not query.get("start_date") or not query.get("end_date"):
        return _reply("NEEDS_INPUT", message="어느 연도나 기간의 가격을 볼까요? 등급과 포장은 몰라도 됩니다.")
    try:
        start, end = (date.fromisoformat(query[k]) for k in ("start_date", "end_date"))
        if not 0 <= (end - start).days <= 365:
            raise ValueError("한 번에 조회할 기간은 최대 366일입니다.")
        filters = {k: str(query[k]).strip() for k in FILTERS if query.get(k) is not None}
        if any(not v or len(v) > 100 for v in filters.values()):
            raise ValueError("선택 조건을 확인해 주세요.")
        if "package_kg" in filters:
            weight = Decimal(filters["package_kg"])
            if not weight.is_finite() or weight <= 0:
                raise ValueError("상자 한 개의 중량은 양수여야 합니다.")
        if filters.get("variety") in {"감귤", "귤", "밀감"}:
            del filters["variety"]
        if filters.get("market") in {"가락", "가락시장", "서울 가락시장", "서울가락시장"}:
            filters["market"] = "서울가락"
    except (ValueError, TypeError, ArithmeticError) as exc:
        return _reply("INVALID_INPUT", message=str(exc))

    rows, sources, seen = [], [], set()
    try:
        for season in SEASONS:
            path = season_price_file(root, season)
            if not path.is_file():
                continue
            snapshot = _snapshot(path)
            selected = [r for r in snapshot if start <= r[0] <= end and r[1]["trade_type"] == "경매"]
            if selected:
                sources.append({"file": path.relative_to(root).as_posix(),
                                "first_date": min(r[0] for r in selected).isoformat(),
                                "last_date": max(r[0] for r in selected).isoformat()})
            for item in selected:
                row = item[1]
                package_weight = Decimal(row["package_kg"])
                if not package_weight.is_finite() or package_weight <= 0 or not row.get("market"):
                    raise ValueError("시장의 표시명과 포장중량을 확인해 주세요.")
                key = tuple(row[k] for k in ("date", "market_code", "variety_code", "grade_code",
                                            "size_code", "package", "trade_type")) + (package_weight,)
                if key in seen:
                    raise ValueError("시즌 자료에 중복 거래규격 집계가 있습니다.")
                seen.add(key)
                rows.append(item)
    except (OSError, ValueError, KeyError, ArithmeticError):
        return _reply("DATA_ERROR", message="시즌 가격 자료를 읽거나 검증하지 못했습니다. 수치를 합산하지 않았습니다.")

    choices = {k: sorted({r[1].get(k, "") for r in rows} - {""}) for k in FILTERS}
    matched = [r for r in rows if all(
        Decimal(r[1][k]) == Decimal(v) if k == "package_kg" else r[1].get(k) == v
        for k, v in filters.items())]
    common = {"period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
              "filters": filters, "choices": choices, "source": {**SOURCE, "files": sources},
              "price_basis": "경매 거래금액 / 거래중량 · 판매수수료 차감 전",
              "warnings": ["품종별 여러 등급·크기·포장을 포함하는 시장 요약입니다. 내 상품의 견적이나 농가 실수취액은 아닙니다.",
                           "확보된 날짜의 거래만 집계합니다. 빈 기간은 보간하지 않으며 미래가격을 예측하지 않습니다."]}
    if not matched:
        return _reply("NO_DATA", **common, message="요청 기간과 선택 조건에 맞는 확보 자료가 없습니다.")

    groups = defaultdict(list)
    for row in matched:
        groups[(row[1]["market_code"], row[1]["market"], row[1]["variety_code"], row[1]["variety"])].append(row)
    ordered = sorted(groups.items(), key=lambda pair: sum(r[2] for r in pair[1]), reverse=True)
    summaries = []
    for (_, market, code, variety), items in ordered[:6]:
        summaries.append({"market": market, "variety": variety, "variety_code": code,
                          "price_per_kg": _price(items), "observed_days": len({r[0] for r in items}),
                          "transaction_count": sum(r[4] for r in items),
                          "first_date": min(r[0] for r in items).isoformat(),
                          "last_date": max(r[0] for r in items).isoformat(),
                          "total_kg": str(sum((r[2] for r in items), Decimal(0)))})
    months = sorted({r[0].strftime("%Y-%m") for r in matched})
    requested_months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        requested_months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    flags = sorted({flag for r in matched for flag in r[1]["quality_flag"].split(";") if flag and flag != "OK"})
    return _reply("OK", **common, groups=summaries, remaining_groups=max(0, len(groups) - 6),
                  coverage={"observed_months": months,
                            "missing_months": sorted(set(requested_months) - set(months)),
                            "observed_days": len({r[0] for r in matched})},
                  quality_flags=flags,
                  flagged_rows=sum(r[1]["quality_flag"] not in {"", "OK"} for r in matched))


def render_price_overview(result):
    if result["status"] != "OK":
        return result.get("message", "조회 결과를 확인해 주세요.")
    period = result["period"]
    lines = [f"**{period['start_date']} ~ {period['end_date']} 확보 가격자료**",
             "", "| 시장 | 품종 | 거래중량 가중 평균 | 관측일 수 |", "|---|---|---:|---:|"]
    for group in result["groups"]:
        market, variety = (group[k].replace("|", "\\|") for k in ("market", "variety"))
        lines.append(f"| {market} | {variety} | {int(group['price_per_kg']):,}원/kg | {group['observed_days']}일 |")
    if result["remaining_groups"]:
        lines += ["", f"거래중량 상위 6개 묶음입니다. 다른 품종 {result['remaining_groups']}개도 조회할 수 있습니다."]
    lines += ["", "관측 월: " + ", ".join(result["coverage"]["observed_months"])]
    if result["coverage"]["missing_months"]:
        lines += ["자료 없는 월: " + ", ".join(result["coverage"]["missing_months"]) + " — 연간 전체 평균이 아닙니다."]
    lines += ["", result["price_basis"] + ". " + result["warnings"][0]]
    if result["flagged_rows"]:
        lines += ["등급·크기 미상 또는 소수 거래 표시가 있는 집계도 포함됩니다. 동일 규격 비교와 구분해 주세요."]
    lines += [f"[출처: {SOURCE['dataset']}]({SOURCE['url']}) · 미래가격 예측 아님"]
    return "\n".join(lines)
