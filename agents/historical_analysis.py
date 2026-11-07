#!/usr/bin/env python3
"""V1-F01: 동일 규격의 과거 날짜쌍을 이용한 지연 출하 가상 손익 분석.

외부 API나 LLM 없이 CSV·Decimal만 사용한다. 분석 결과는 예측이 아니라
'과거 두 날짜에 관측된 시장가격으로 같은 물량을 판매했다면'이라는 가정이다.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from agents.baseline import DEFAULT_PRICES_FILE, load_price_observations, to_decimal
from agents.evidence_quality import assess_evidence_quality

MARKET_CODE = "110001"


def _number(value: Decimal, digits: int) -> str:
    """반올림 전 Decimal을 유지하고 출력 직전에만 소수 자릿수를 정한다."""
    unit = Decimal(1).scaleb(-digits)
    return format(value.quantize(unit, rounding=ROUND_HALF_UP), f".{digits}f") if digits else format(value.quantize(Decimal(1), rounding=ROUND_HALF_UP), "f")


def _date(value: str | date | None, label: str) -> date | None:
    """날짜는 YYYY-MM-DD만 허용한다. 조회 상한을 현재 날짜로 추측해서 채우지 않는다."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"{label}은 YYYY-MM-DD 형식이어야 합니다.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}은 유효한 YYYY-MM-DD 날짜여야 합니다.") from exc


def _validated_inputs(args: dict, analysis_date: date | None) -> dict:
    """LLM이 제시한 물량·기간·비용에 도메인 범위 검사를 적용한다."""
    variety = args["variety"]
    grade = args["grade"]
    size = args["size"]
    if not all(isinstance(x, str) and x.strip() for x in (variety, grade, size)):
        raise ValueError("품종·등급·크기는 모두 정확하게 입력해야 합니다. 임의로 '미상'을 채우지 마세요.")
    variety, grade, size = (x.strip() for x in (variety, grade, size))
    market = args.get("market_code", MARKET_CODE)
    if market != MARKET_CODE:
        raise ValueError("현재는 서울가락시장(110001)만 분석할 수 있습니다.")

    pkg = to_decimal(args["package_kg"], "포장중량")
    quantity = to_decimal(args["quantity_kg"], "출하 물량")
    cost = to_decimal(args["daily_storage_cost_krw"], "하루 추가 보관비")
    loss = to_decimal(args["loss_percent"], "누적 감량률")
    days = args["delay_days"]
    min_count = args.get("min_transactions_per_day", 5)
    if min_count is None:
        min_count = 5
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("보관일수는 1 이상의 정수여야 합니다.")
    if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 1:
        raise ValueError("최소 거래건수는 1 이상의 정수여야 합니다.")
    if pkg <= 0 or quantity <= 0:
        raise ValueError("포장중량과 출하 물량은 0보다 커야 합니다.")
    if cost < 0:
        raise ValueError("하루 추가 보관비는 0 이상이어야 합니다.")
    if not Decimal(0) <= loss < Decimal(100):
        raise ValueError("누적 감량률은 0% 이상 100% 미만이어야 합니다.")

    current_day = _date(analysis_date, "분석 기준일") or date.today()
    upper_bound = _date(args.get("as_of_date"), "조회 상한일")
    if upper_bound is not None and upper_bound > current_day:
        # '미래로 조회할래'라는 질문이 데이터 선행 조회를 허용하는 것은 아니다.
        upper_bound = current_day
    elif upper_bound is None:
        upper_bound = current_day
    return {
        "variety": variety, "grade": grade, "size": size,
        "package_kg": pkg, "quantity_kg": quantity, "delay_days": days,
        "daily_storage_cost_krw": cost, "loss_percent": loss,
        "min_transactions_per_day": min_count, "as_of_date": upper_bound,
        "analysis_date": current_day, "market_code": MARKET_CODE,
    }


def analyze_historical_delays(
    args: dict,
    prices_file: Path = DEFAULT_PRICES_FILE,
    *,
    analysis_date: date | None = None,
) -> dict:
    """과거 모든 달력상 정확히 N일 간격의 경락가격으로 가상 손익을 계산한다.

    반환된 ``numeric``에는 보고·평가용 요약, ``pairs``에는 각 날짜쌍의
    포함 여부와 가상 손익이 들어간다. 제외된 구간도 재현 가능하게 남긴다.
    """
    c = _validated_inputs(args, analysis_date)
    observations = load_price_observations(
        prices_file,
        variety=c["variety"], grade=c["grade"], size=c["size"],
        package_kg=c["package_kg"], market_code=c["market_code"],
        as_of=c["as_of_date"],
    )
    by_day = {o.date: o for o in observations}
    qty = c["quantity_kg"]
    remaining = qty * (Decimal(1) - c["loss_percent"] / Decimal(100))
    storage_total = Decimal(c["delay_days"]) * c["daily_storage_cost_krw"]

    candidate_rows: list[dict] = []
    valid_differences: list[Decimal] = []
    excluded_count = 0
    profit_count = loss_count = tie_count = 0
    for start in observations:
        # 거래일의 순번이 아니라 달력상 정확히 delay_days일 뒤만 연결한다.
        try:
            end_date = start.date + timedelta(days=c["delay_days"])
        except OverflowError:
            raise ValueError("보관기간이 날짜 계산 범위를 초과합니다.") from None
        end = by_day.get(end_date)
        if end is None:
            continue
        # 과거 각 구간의 시작 가격은 그 구간의 시작 날짜 가격이다.
        # 현재 관측가격 하나를 모든 과거 구간에 재사용하면 안 된다.
        start_gross = qty * start.price_per_kg
        end_net = remaining * end.price_per_kg - storage_total
        difference = end_net - start_gross
        percentage = (end.price_per_kg / start.price_per_kg - Decimal(1)) * 100
        included = (start.transaction_count >= c["min_transactions_per_day"] and
                    end.transaction_count >= c["min_transactions_per_day"])
        if included:
            valid_differences.append(difference)
            profit_count += difference > 0
            loss_count += difference < 0
            tie_count += difference == 0
        else:
            excluded_count += 1
        candidate_rows.append({
            "start_date": start.date.isoformat(),
            "end_date": end.date.isoformat(),
            "start_price_krw_per_kg": _number(start.price_per_kg, 2),
            "end_price_krw_per_kg": _number(end.price_per_kg, 2),
            "start_transactions": start.transaction_count,
            "end_transactions": end.transaction_count,
            "price_change_percent": _number(percentage, 2),
            "difference_krw": _number(difference, 0),
            "included": included,
            "exclusion_reason": None if included else "low_sample",
        })

    valid_differences.sort()
    n = len(valid_differences)
    median = ((valid_differences[(n - 1) // 2] + valid_differences[n // 2]) / Decimal(2)) if n else None
    quality = assess_evidence_quality(
        observations=observations,
        candidate_pair_count=len(candidate_rows),
        excluded_low_sample_pair_count=excluded_count,
        valid_pair_count=n,
        analysis_date=c["analysis_date"],
        min_transactions_per_day=c["min_transactions_per_day"],
    )
    numeric = {
        "candidate_pair_count": len(candidate_rows),
        "excluded_low_sample_pair_count": excluded_count,
        "valid_pair_count": n,
        "profit_pair_count": profit_count,
        "loss_pair_count": loss_count,
        "tie_pair_count": tie_count,
        "min_difference_krw": _number(valid_differences[0], 0) if n else None,
        "median_difference_krw": _number(median, 0) if n else None,
        "max_difference_krw": _number(valid_differences[-1], 0) if n else None,
        "last_observed_date": quality["last_observed_date"],
        "days_since_last_observation": quality["days_since_last_observation"],
    }
    return {
        "simulation_type": "historical_counterfactual",
        "is_forecast": False,
        "conditions": {
            "market_code": c["market_code"], "market": "서울가락",
            "variety": c["variety"], "grade": c["grade"], "size": c["size"],
            "package": "상자", "package_kg": _number(c["package_kg"], 3),
            "quantity_kg": _number(qty, 2), "delay_days": c["delay_days"],
            "daily_storage_cost_krw": _number(c["daily_storage_cost_krw"], 0),
            "loss_percent": _number(c["loss_percent"], 2),
            "as_of_date": c["as_of_date"].isoformat(),
            "analysis_date": c["analysis_date"].isoformat(),
            "min_transactions_per_day": c["min_transactions_per_day"],
        },
        "numeric": numeric,
        "pairs": candidate_rows,
        "status": quality["status"],
        "warnings": quality["warnings"],
        "warning_messages": quality["warning_messages"],
        "quality": quality,
    }


def format_historical_report(result: dict, *, quality_only: bool = False) -> str:
    """LLM의 재계산을 거치지 않고 도구가 산출한 숫자만 한국어로 표시한다."""
    c, n = result["conditions"], result["numeric"]
    lines = [
        "[과거 지연 출하 가상 손익 및 근거 품질 분석]" if not quality_only else "[과거 분석 근거 품질 점검]",
        f"시장: {c['market']} | 품종: {c['variety']} | 등급: {c['grade']} | 크기: {c['size']} | 상자 {c['package_kg']}kg",
        f"물량: {c['quantity_kg']}kg | 지연: {c['delay_days']}일 | 하루 보관비: {c['daily_storage_cost_krw']}원 | 기간 누적 감량률: {c['loss_percent']}%",
        f"조회 상한: {c['as_of_date']} | 분석 기준일: {c['analysis_date']} | 마지막 가격 관측: {n['last_observed_date'] or '없음'}",
        f"과거 후보 날짜쌍: {n['candidate_pair_count']}개 | 낮은 거래건수로 제외: {n['excluded_low_sample_pair_count']}개 | 유효: {n['valid_pair_count']}개",
        f"근거 품질 상태: {result['status']} (자료 제약 안내용 등급이며 통계적 신뢰수준이 아님)",
    ]
    if not quality_only:
        if n["valid_pair_count"]:
            lines += [
                f"가상 이익: {n['profit_pair_count']}구간 | 가상 손실: {n['loss_pair_count']}구간 | 손익 동일: {n['tie_pair_count']}구간",
                f"가상 차액 최소·중앙값·최대: {int(n['min_difference_krw']):,}원 / {int(n['median_difference_krw']):,}원 / {int(n['max_difference_krw']):,}원",
                "[달력상 정확한 지연일수별 과거 관측 구간 — 가상 손익]",
            ]
            for p in result["pairs"]:
                if not p["included"]:
                    continue
                diff = int(p["difference_krw"])
                lines.append(
                    f"{p['start_date']} → {p['end_date']}: "
                    f"{p['start_price_krw_per_kg']} → {p['end_price_krw_per_kg']}원/kg "
                    f"({p['price_change_percent']}%), 차액 {diff:+,}원 "
                    f"[거래 {p['start_transactions']}건/{p['end_transactions']}건]"
                )
        else:
            lines.append("분석 가능한 과거 구간이 없습니다. 해당 조건으로 이익·손실을 산출하지 않습니다.")
    lines.extend("※ " + result["warning_messages"][code] for code in result["warnings"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 없이 과거 출하 지연 가상 손익 분석")
    parser.add_argument("--prices-file", type=Path, default=DEFAULT_PRICES_FILE)
    parser.add_argument("--variety", required=True)
    parser.add_argument("--grade", required=True)
    parser.add_argument("--size", required=True)
    parser.add_argument("--package-kg", type=float, required=True)
    parser.add_argument("--quantity-kg", type=float, required=True)
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--storage-cost-per-day", type=float, required=True)
    parser.add_argument("--loss-pct", type=float, required=True)
    parser.add_argument("--as-of-date")
    parser.add_argument("--analysis-date", help="재현용 분석 기준일, 기본값은 실행일")
    parser.add_argument("--min-transactions", type=int, default=5)
    parser.add_argument("--quality-only", action="store_true")
    parser.add_argument("--json", action="store_true", help="기계 판독용 JSON 출력")
    a = parser.parse_args()
    try:
        result = analyze_historical_delays(
            {"variety": a.variety, "grade": a.grade, "size": a.size,
             "package_kg": a.package_kg, "quantity_kg": a.quantity_kg,
             "delay_days": a.days, "daily_storage_cost_krw": a.storage_cost_per_day,
             "loss_percent": a.loss_pct, "as_of_date": a.as_of_date,
             "min_transactions_per_day": a.min_transactions},
            a.prices_file,
            analysis_date=_date(a.analysis_date, "분석 기준일"),
        )
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_historical_report(result, quality_only=a.quality_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
