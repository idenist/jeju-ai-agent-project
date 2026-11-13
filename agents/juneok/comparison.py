"""Deterministic comparison of user-supplied shipment assumptions.

The zero-fee case is independently regressed against the team's 3t example.
The extended cost contract is a personal implementation, not copied team code.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING

CATEGORIES = {"commission", "sorting", "packaging", "transport", "storage", "other"}
TOP_FIELDS = {"quantity_kg", "current_price_per_kg", "price_basis", "current_fee_percent",
              "cost_policy", "included_costs", "current_costs", "storage_available",
              "shipping_flexible", "options"}
OPTION_FIELDS = {"id", "delay_days", "loss_percent", "daily_storage_cost", "fee_percent",
                 "future_price_per_kg", "costs", "loss_confirmed_for_period"}


def decimal(value, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name}: 숫자를 입력하세요.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name}: 유효한 숫자를 입력하세요.") from exc
    if not result.is_finite():
        raise ValueError(f"{name}: 유한한 숫자를 입력하세요.")
    return result


def fixed(value: Decimal) -> str:
    return format(value, "f")


def validate_partial(scenario: dict) -> None:
    if not isinstance(scenario, dict) or set(scenario) - TOP_FIELDS:
        raise ValueError("허용되지 않은 시나리오 필드입니다.")
    for field in ("quantity_kg", "current_price_per_kg", "current_fee_percent"):
        if scenario.get(field) is not None:
            v = decimal(scenario[field], field)
            if v < 0 or (field in {"quantity_kg", "current_price_per_kg"} and v == 0):
                raise ValueError(f"{field}: 양수여야 합니다.")
            if field == "current_fee_percent" and v >= 100:
                raise ValueError("수수료율은 0 이상 100 미만입니다.")
    if scenario.get("price_basis") not in {None, "commission_net", "gross"}:
        raise ValueError("가격 기준은 commission_net 또는 gross입니다.")
    if scenario.get("cost_policy") not in {None, "equal_excluded", "itemized"}:
        raise ValueError("기타 비용은 동일 제외 가정 또는 항목별 입력을 선택하세요.")
    for field in ("storage_available", "shipping_flexible"):
        if scenario.get(field) is not None and type(scenario[field]) is not bool:
            raise ValueError(f"{field}: 예/아니오/미확인 중 하나입니다.")
    included = scenario.get("included_costs")
    if included is not None and (not isinstance(included, list) or
            any(not isinstance(x, str) or x not in CATEGORIES for x in included) or len(set(included)) != len(included)):
        raise ValueError("단가 포함비용 항목이 잘못됐습니다.")
    options = scenario.get("options", [])
    if not isinstance(options, list) or len(options) > 3:
        raise ValueError("지연 대안은 최대 3개입니다.")
    seen = set()
    for option in options:
        if not isinstance(option, dict) or set(option) - OPTION_FIELDS:
            raise ValueError("허용되지 않은 대안 필드입니다.")
        name = option.get("id")
        if not isinstance(name, str) or not name.strip() or len(name) > 40 or name in seen:
            raise ValueError("대안 ID는 중복되지 않는 짧은 문자열이어야 합니다.")
        seen.add(name)
        days = option.get("delay_days")
        if days is not None and (type(days) is not int or not 1 <= days <= 365):
            raise ValueError("보관기간은 1~365 사이 정수입니다. 실제 가능 기간은 별도 확인합니다.")
        for field in ("loss_percent", "fee_percent", "daily_storage_cost", "future_price_per_kg"):
            if option.get(field) is not None:
                v = decimal(option[field], field)
                if v < 0 or (field.endswith("percent") and v >= 100):
                    raise ValueError(f"{field}: 음수 또는 100% 이상의 비율은 사용할 수 없습니다.")
        confirmed = option.get("loss_confirmed_for_period")
        if confirmed is not None and type(confirmed) is not bool:
            raise ValueError("기간별 감모 확인은 예/아니오입니다.")
    # Validate itemized entries even while other required fields remain unknown.
    for costs in [scenario.get("current_costs"), *(o.get("costs") for o in options)]:
        if costs is None:
            continue
        if not isinstance(costs, list) or len(costs) > 6:
            raise ValueError("비용 항목 목록이 잘못됐습니다.")
        categories = set()
        for item in costs:
            if not isinstance(item, dict) or set(item) != {"category", "amount_krw"}:
                raise ValueError("비용 항목은 category와 amount_krw가 필요합니다.")
            category = item["category"]
            if category not in CATEGORIES - {"commission"} or category in categories:
                raise ValueError("비용 항목 중복 또는 미지원 항목입니다. 수수료는 비율 필드에 입력하세요.")
            categories.add(category)
            if decimal(item["amount_krw"], "비용") < 0:
                raise ValueError("비용은 음수일 수 없습니다.")
            if category in (included or []):
                raise ValueError(f"{category}: 단가에 이미 포함한 비용을 다시 차감할 수 없습니다.")
    # Reject cross-field contradictions before an atomic state update can commit.
    all_fees = [scenario.get("current_fee_percent"), *(o.get("fee_percent") for o in options)]
    if scenario.get("price_basis") == "commission_net" and any(
            v is not None and decimal(v, "수수료") != 0 for v in all_fees):
        raise ValueError("수수료 차감 후 단가에서 수수료를 다시 차감할 수 없습니다.")
    if scenario.get("price_basis") == "gross" and "commission" in (included or []):
        raise ValueError("수수료 포함 여부와 차감 전 가격 기준이 충돌합니다.")
    if "storage" in (included or []) and any(
            o.get("daily_storage_cost") is not None and decimal(o["daily_storage_cost"], "보관비") > 0 for o in options):
        raise ValueError("단가에 포함한 보관비를 다시 차감할 수 없습니다.")
    for option in options:
        if option.get("daily_storage_cost") is not None and decimal(option["daily_storage_cost"], "보관비") > 0:
            if any(x["category"] == "storage" and decimal(x["amount_krw"], "비용") > 0 for x in option.get("costs") or []):
                raise ValueError("보관비를 중복 차감할 수 없습니다.")
    if scenario.get("cost_policy") == "equal_excluded" and any(
            decimal(x["amount_krw"], "비용") != 0
            for entries in [scenario.get("current_costs"), *(o.get("costs") for o in options)]
            for x in entries or []):
        raise ValueError("동일 비용 제외 가정과 별도 차감 비용이 충돌합니다.")


def compare_shipment_options(scenario: dict) -> dict:
    snapshot = deepcopy(scenario)
    try:
        validate_partial(snapshot)
        required = [k for k in ("quantity_kg", "current_price_per_kg", "price_basis", "cost_policy", "included_costs")
                    if snapshot.get(k) is None]
        options = snapshot.get("options", [])
        if not options:
            required.append("options")
        for option in options:
            required += [f"options.{option['id']}.{k}" for k in
                         ("delay_days", "loss_percent", "daily_storage_cost") if option.get(k) is None]
        basis = snapshot.get("price_basis")
        if basis == "gross":
            if snapshot.get("current_fee_percent") is None:
                required.append("current_fee_percent")
            required += [f"options.{o['id']}.fee_percent" for o in options if o.get("fee_percent") is None]
        if snapshot.get("cost_policy") == "itemized":
            if snapshot.get("current_costs") is None:
                required.append("current_costs")
            required += [f"options.{o['id']}.costs" for o in options if o.get("costs") is None]
        if required:
            return {"status": "NEEDS_INPUT", "missing_fields": required, "input": snapshot,
                    "message": "미확인 값은 0이 아닙니다. 실제 값 또는 사용자가 정한 가정을 확인해 주세요."}
        included = set(snapshot["included_costs"])
        all_fees = [snapshot.get("current_fee_percent"), *(o.get("fee_percent") for o in options)]
        if basis == "commission_net" and any(v is not None and decimal(v, "수수료") != 0 for v in all_fees):
            raise ValueError("수수료 차감 후 단가에서 수수료를 다시 차감할 수 없습니다.")
        if basis == "gross" and "commission" in included:
            raise ValueError("수수료 포함 여부와 차감 전 가격 기준이 충돌합니다.")
        if any(decimal(o["daily_storage_cost"], "보관비") > 0 for o in options) and "storage" in included:
            raise ValueError("단가에 포함한 보관비를 다시 차감할 수 없습니다.")
        def costs(entries):
            return sum((decimal(x["amount_krw"], "비용") for x in (entries or [])), Decimal(0))
        if snapshot["cost_policy"] == "equal_excluded":
            if any(costs(x) != 0 for x in [snapshot.get("current_costs"), *(o.get("costs") for o in options)]):
                raise ValueError("동일 비용 제외 가정과 별도 차감 비용이 충돌합니다.")
        else:
            for option in options:
                if any(x["category"] == "storage" and decimal(x["amount_krw"], "비용") > 0 for x in option["costs"]) and decimal(option["daily_storage_cost"], "보관비") > 0:
                    raise ValueError("보관비를 일별 비용과 항목별 비용에 중복 입력했습니다.")
        q = decimal(snapshot["quantity_kg"], "물량")
        p = decimal(snapshot["current_price_per_kg"], "현재 단가")
        fee0 = decimal(snapshot.get("current_fee_percent") or 0, "수수료") / 100 if basis == "gross" else Decimal(0)
        n0 = q * p * (1 - fee0) - costs(snapshot.get("current_costs"))
        results = []
        for option in options:
            days = option["delay_days"]
            r = decimal(option["loss_percent"], "감모") / 100
            fee = decimal(option.get("fee_percent") or 0, "수수료") / 100 if basis == "gross" else Decimal(0)
            remaining = q * (1 - r)
            storage = decimal(option["daily_storage_cost"], "보관비") * days
            total_cost = storage + costs(option.get("costs"))
            # If immediate net proceeds are negative, zero future price can already break even.
            threshold = max(Decimal(0), (n0 + total_cost) / (remaining * (1 - fee)))
            future = option.get("future_price_per_kg")
            net = remaining * decimal(future, "가정 단가") * (1 - fee) - total_cost if future is not None else None
            results.append({"id": option["id"], "delay_days": days,
                            "remaining_kg": fixed(remaining), "storage_cost_krw": fixed(storage),
                            "total_cost_krw": fixed(total_cost), "break_even_price_per_kg": fixed(threshold),
                            "minimum_whole_krw_per_kg": str(threshold.to_integral_value(rounding=ROUND_CEILING)),
                            "hypothetical_proceeds_krw": fixed(net) if net is not None else None,
                            "difference_krw": fixed(net - n0) if net is not None else None})
        constraints = [k for k in ("storage_available", "shipping_flexible") if snapshot.get(k) is None]
        conflicts = [k for k in ("storage_available", "shipping_flexible") if snapshot.get(k) is False]
        return {"status": "OK", "input": snapshot, "current_proceeds_krw": fixed(n0), "options": results,
                "feasibility": {"status": "CONFLICT" if conflicts else "UNCONFIRMED" if constraints else "CONFIRMED_BY_USER",
                                "unknown": constraints, "conflicts": conflicts},
                "warnings": ["사용자가 제공한 조건에 따른 비교이며 미래가격 예측이 아닙니다.",
                             "입력·포함한 비용만 반영한 비교금액이며 농가 전체 순이익이 아닙니다.",
                             "기간별 감모율은 각 보관기간 전체의 사용자 가정입니다."],
                "assumptions": ["보관비 외 기타 고정비용은 동일하다고 확인해 비교에서 제외했습니다."] if snapshot["cost_policy"] == "equal_excluded" else [],
                "is_future_forecast": False, "recommended_option": None}
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return {"status": "INVALID_INPUT", "message": str(exc), "input": snapshot}
