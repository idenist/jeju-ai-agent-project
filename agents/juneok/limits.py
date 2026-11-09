"""Solve one economic boundary with the existing comparison cost contract."""
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
from .comparison import validate_partial, decimal, fixed
from agents.common.calculation_service import compare_shipment_options


def solve_offer_limits(scenario, option_id, quoted_price_per_kg, solve_for):
    try:
        validate_partial(scenario)
        if solve_for not in {"loss_percent", "daily_storage_cost"}:
            raise ValueError("허용 감모율 또는 하루 추가 보관비를 선택하세요.")
        options = scenario.get("options", [])
        if option_id is None and len(options) == 1: option_id = options[0]["id"]
        selected = next((o for o in options if o["id"] == option_id), None)
        if selected is None:
            return {"status": "NEEDS_INPUT", "missing_fields": ["option_id"], "message": "역산할 대안을 선택하세요."}
        if quoted_price_per_kg is None:
            return {"status": "NEEDS_INPUT", "missing_fields": ["quoted_price_per_kg"], "message": "제안 단가 또는 사용자가 정한 미래 단가 가정을 알려주세요."}
        quote = decimal(quoted_price_per_kg, "제안 단가")
        if quote < 0: raise ValueError("제안 단가는 음수일 수 없습니다.")
        candidate = deepcopy(scenario); option = deepcopy(selected)
        # Only the solved variable is neutralized for algebra, never saved as a fact.
        option[solve_for] = 0
        option["future_price_per_kg"] = str(quote)
        candidate["options"] = [option]
        if solve_for == "daily_storage_cost" and ("storage" in (scenario.get("included_costs") or []) or any(x["category"] == "storage" and decimal(x["amount_krw"], "보관비") > 0 for x in selected.get("costs") or [])):
            raise ValueError("보관비가 단가 또는 별도 비용에 포함되어 있어 일별 보관비를 추가 역산할 수 없습니다.")
        base = compare_shipment_options(candidate)
        if base["status"] != "OK": return {**base, "input": deepcopy(scenario)}
        now = decimal(base["current_proceeds_krw"], "현재 비교금액")
        fee = decimal(selected.get("fee_percent") or 0, "수수료") / 100 if scenario["price_basis"] == "gross" else Decimal(0)
        net_price = quote * (1 - fee)
        q = decimal(scenario["quantity_kg"], "물량")
        other = sum((decimal(x["amount_krw"], "비용") for x in selected.get("costs") or []), Decimal(0))
        result = {"status": "OK", "analysis_type": "offer_limits", "option_id": option_id, "solve_for": solve_for,
                  "quoted_price_per_kg": fixed(quote), "delay_days": selected["delay_days"], "input": deepcopy(scenario),
                  "current_proceeds_krw": fixed(now), "feasibility": base["feasibility"], "assumptions": base["assumptions"],
                  "warnings": [*base["warnings"], "제안 단가는 사용자 입력/가정입니다. 한 변수만 바꾸고 나머지 조건을 고정한 경제성 경계입니다.", "감모 상한과 보관비 상한을 동시에 적용할 수 있다는 뜻이 아닙니다. 실제 저장·계약 가능성을 별도로 확인하세요."], "is_future_forecast": False}
        if solve_for == "loss_percent":
            required = now + other + decimal(selected["daily_storage_cost"], "보관비") * selected["delay_days"]
            if net_price == 0:
                boundary, kind = (None, "ALL_VALID_VALUES") if required <= 0 else (None, "NO_FEASIBLE_VALUE")
            else:
                boundary = (1 - required / (q * net_price)) * 100
                kind = "NO_FEASIBLE_VALUE" if boundary < 0 else "ALL_VALID_VALUES" if boundary >= 100 else "FINITE_LIMIT"
            result.update(unit="%", domain="0 이상 100 미만", fixed_loss_percent=None, fixed_daily_storage_cost=selected["daily_storage_cost"])
            step = Decimal("0.01")
        else:
            remaining = q * (1 - decimal(selected["loss_percent"], "감모") / 100)
            boundary = (remaining * net_price - other - now) / selected["delay_days"]
            kind = "NO_FEASIBLE_VALUE" if boundary < 0 else "FINITE_LIMIT"
            result.update(unit="원/일 · 전체 물량", domain="0 이상", fixed_loss_percent=selected["loss_percent"], fixed_daily_storage_cost=None)
            step = Decimal(1)
        result.update(boundary_kind=kind, exact_boundary=fixed(boundary) if boundary is not None else None,
                      conservative_limit=fixed(boundary.quantize(step, rounding=ROUND_FLOOR)) if kind == "FINITE_LIMIT" else None)
        return result
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}
