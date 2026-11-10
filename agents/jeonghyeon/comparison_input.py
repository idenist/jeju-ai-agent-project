"""B안 가정 단가를 팀 손익 비교 함수(compare_shipment_options)의 입력으로 바꾼다.

첫 버전은 '지금 출하(A) vs 보관 후 출하(B)'만 다룬다. 손익분기 단가와 차액은 팀 함수가 계산한다.
"""
from __future__ import annotations

from decimal import Decimal

from .price_history import InputError, number
from .price_scenario import execute_scenario_tool

# integration/agents의 agents/juneok/comparison.py OPTION_FIELDS 중 이 변환이 채우는 필드
OPTION_KEYS = ("id", "delay_days", "loss_percent", "daily_storage_cost", "future_price_per_kg")
MANUAL = "B안 단가를 직접 입력해 손익 비교를 이어가 주세요."
SHORT = {"min": "가장 크게 내린 사례", "median": "중앙값", "max": "가장 크게 오른 사례"}


def _optional_number(value, name: str, *, upper: Decimal | None = None) -> str | None:
    # 모르는 값은 0으로 바꾸지 않고 비워 두어 팀 함수가 다시 묻게 한다.
    if value is None:
        return None
    if isinstance(value, bool):
        raise InputError(f"{name}: 숫자를 입력해 주세요.")
    try:
        result = number(value)
    except ValueError as exc:
        raise InputError(f"{name}: 숫자를 입력해 주세요.") from exc
    if result < 0 or (upper is not None and result >= upper):
        raise InputError(f"{name}: 범위를 벗어난 값입니다.")
    return format(result.normalize(), "f")


def to_comparison_input(scenario: dict, *, delay_days, loss_percent=None, daily_storage_cost=None) -> dict:
    """price_scenario 결과를 팀 함수의 current_price_per_kg + options 와, id로 연결되는 근거로 나눈다."""
    # 과거 가격을 찾는 간격(A·B 판매일 차이)과 팀 함수의 보관일(delay_days)은 다른 값일 수 있다.
    if type(delay_days) is not int or not 1 <= delay_days <= 365:
        raise InputError("B안 추가 보관일은 1~365 사이 정수로 입력해 주세요.")
    loss = _optional_number(loss_percent, "감량률(%)", upper=Decimal(100))
    storage = _optional_number(daily_storage_cost, "하루 보관비(원)")

    status = scenario.get("status")
    if status == "ERROR":
        return scenario
    if status != "OK":
        # 간격이 바뀌어 과거 사례를 못 찾으면 이전 가정 단가를 재사용하지 않는다.
        manual = status in {"INTERVAL_NOT_AVAILABLE", "NO_DATA", "NO_CLEAN_COMPARISON", "AMBIGUOUS_CODES"}
        result = {"status": status, "comparison_input": None, "evidence": {},
                  "manual_price_required": manual,
                  "message": f"{scenario.get('message', '')} {MANUAL}".strip() if manual else scenario.get("message")}
        if status == "NEEDS_CLARIFICATION":
            result.update(missing_fields=scenario["missing_fields"], choices=scenario.get("choices", {}))
        return result

    interval = scenario["interval_days"]
    options, evidence = [], {}
    for item in scenario["scenarios"]:
        key = f"past_{item['basis']}_{interval}d"
        options.append(dict(zip(OPTION_KEYS, (key, delay_days, loss, storage, item["option_b_price_per_kg"]))))
        evidence[key] = {"price_type": "assumption", "label": item["label"], "change_pct": item["change_pct"],
                         "price_interval_days": interval, "delay_days": delay_days,
                         "clean_pairs": scenario["clean_pairs"], "base_date_range": scenario["base_date_range"],
                         "conditions": scenario["conditions"], "source": scenario["source"],
                         "summary": f"B안 {item['option_b_price_per_kg']}원/kg · 동일 규격 {interval}일 과거 "
                                    f"{scenario['clean_pairs']}쌍의 {SHORT[item['basis']]} 적용 · 가정"}
    evidence["current"] = {"price_type": "assumption", "price_source": "사용자 입력",
                           "sale_date": scenario["option_a"]["sale_date"]}
    return {"status": "OK",
            "comparison_input": {"current_price_per_kg": scenario["option_a"]["price_per_kg"], "options": options},
            "evidence": evidence, "cautions": scenario["cautions"],
            "message": "quantity_kg, price_basis, cost_policy, included_costs는 팀 손익 함수 입력에서 함께 정합니다."}


def build_comparison_input(args: dict, *, delay_days, loss_percent=None, daily_storage_cost=None, **paths) -> dict:
    try:
        scenario = execute_scenario_tool(args, **paths)
        return to_comparison_input(scenario, delay_days=delay_days, loss_percent=loss_percent,
                                   daily_storage_cost=daily_storage_cost)
    except InputError as exc:
        return {"status": "ERROR", "error_kind": "INVALID_INPUT", "error": str(exc)}
