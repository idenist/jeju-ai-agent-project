"""Finite sensitivity grid over explicit user hypotheses; no price forecasting."""
from __future__ import annotations

from copy import deepcopy
from .comparison import decimal
from agents.common.calculation_service import compare_shipment_options


def compare_sensitivity(scenario: dict, option_id: str, loss_values: list, price_values: list | None) -> dict:
    try:
        if not isinstance(option_id, str):
            raise ValueError("비교할 보관 대안 이름을 확인하세요.")
        options = [o for o in scenario.get("options", []) if o["id"] == option_id]
        if len(options) != 1:
            raise ValueError("현재 조건에 있는 보관 대안을 선택하세요.")
        if loss_values is None:
            return {"status": "NEEDS_INPUT", "missing_fields": ["loss_values"],
                    "message": "사용할 감모 가정 목록을 직접 정해 주세요. 자동으로 가정을 만들지 않습니다."}
        def values(items, name, loss=False):
            if not isinstance(items, list) or not 1 <= len(items) <= 7:
                raise ValueError(f"{name}은 사용자가 정한 1~7개의 값이어야 합니다.")
            parsed = [decimal(v, name) for v in items]
            if len(set(parsed)) != len(parsed) or any(v < 0 or (loss and v >= 100) for v in parsed):
                raise ValueError(f"{name} 범위·중복을 확인하세요. 감모는 0 이상 100 미만이며 가격은 0 이상입니다.")
            return parsed
        losses = values(loss_values, "기간 전체 감모율", True)
        prices = values(price_values, "가정 미래 단가") if price_values is not None else [None]
        rows = []
        for loss in losses:
            for price in prices:
                candidate = deepcopy(scenario)
                candidate["options"] = [deepcopy(options[0])]
                option = candidate["options"][0]
                option.update(loss_percent=str(loss), future_price_per_kg=str(price) if price is not None else None,
                              loss_confirmed_for_period=True)
                result = compare_shipment_options(candidate)
                if result["status"] != "OK":
                    return result
                calculated = result["options"][0]
                rows.append({"loss_percent": str(loss), "future_price_per_kg": str(price) if price is not None else None,
                             **{k: calculated[k] for k in ["remaining_kg", "minimum_whole_krw_per_kg", "hypothetical_proceeds_krw", "difference_krw"]}})
        return {"status": "OK", "analysis_type": "sensitivity", "option_id": option_id,
                "delay_days": options[0]["delay_days"], "rows": rows, "current_proceeds_krw": result["current_proceeds_krw"],
                "base_input": deepcopy(scenario), "feasibility": result["feasibility"],
                "assumptions": ["감모율·미래 단가 목록은 사용자가 지정한 가정입니다. 보관기간과 나머지 조건은 고정했습니다."],
                "warnings": result["warnings"] + ["이 표는 확률·신뢰구간·가격 예측이나 최적 출하일 추천이 아닙니다. 가격·감모·비용은 서로 영향을 줄 수 있으나 여기서는 지정한 조합을 비교합니다."],
                "is_future_forecast": False}
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}
