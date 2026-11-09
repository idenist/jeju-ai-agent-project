"""Explain finite user hypotheses without ranking or interpolating them."""
from .comparison import compare_shipment_options, decimal
from .sensitivity import compare_sensitivity


def decision_conditions(scenario, option_id=None, loss_values=None, price_values=None):
    base = compare_shipment_options(scenario)
    if option_id is None and len(scenario.get("options", [])) == 1:
        option_id = scenario["options"][0]["id"]
    grid = None
    if loss_values is not None or price_values is not None:
        grid = compare_sensitivity(scenario, option_id, loss_values, price_values)
        if grid["status"] != "OK":
            return grid
        base = grid
    elif base["status"] != "OK":
        return base
    if option_id not in {o["id"] for o in scenario.get("options", [])}:
        return {"status": "NEEDS_INPUT", "message": "설명할 보관 대안을 선택하세요.", "missing_fields": ["option_id"]}
    option = next((o for o in base.get("options", []) if o["id"] == option_id), None)
    rows = grid["rows"] if grid else [{"loss_percent": next(o for o in scenario["options"] if o["id"] == option_id)["loss_percent"],
        "future_price_per_kg": next(o for o in scenario["options"] if o["id"] == option_id).get("future_price_per_kg"),
        **option}]
    cases = []
    for row in rows:
        diff = row.get("difference_krw")
        outcome = "PRICE_UNKNOWN" if diff is None else "HIGHER" if decimal(diff, "차이") > 0 else "LOWER" if decimal(diff, "차이") < 0 else "EQUAL"
        cases.append({"loss_percent": str(row["loss_percent"]), "future_price_per_kg": row.get("future_price_per_kg"),
                      "minimum_whole_krw_per_kg": row["minimum_whole_krw_per_kg"], "difference_krw": diff,
                      "outcome": outcome})
    thresholds = [decimal(r["minimum_whole_krw_per_kg"], "최소단가") for r in cases]
    return {"status": "OK", "analysis_type": "decision_conditions", "option_id": option_id,
            "cases": cases, "minimum_range": {"low": str(min(thresholds)), "high": str(max(thresholds))},
            "feasibility": base["feasibility"], "assumptions": base["assumptions"],
            "warnings": [*base["warnings"], "입력한 조합 안에서만 비교합니다. 조합 사이의 값·확률·최적 대안을 추정하지 않습니다."],
            "is_future_forecast": False, "recommended_option": None}
