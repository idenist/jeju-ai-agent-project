"""Compare two future shipments using the existing option calculator."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING

from agents.common.calculation_service import compare_shipment_options
from agents.common.data_contract import verify_integrated_price_sources
from agents.juneok.comparison import decimal, fixed
from agents.juneok.config import PROJECT_ROOT


def _engine_option(option, template):
    costs = option.get("other_cost_krw")
    return {**template, "fee_percent": option.get("fee_percent"),
            "costs": None if costs is None else [{"category": "other", "amount_krw": costs}]}


def compare_future_pair(*, quantity, price_basis, start_date, option_a, option_b,
                        price_a, price_b=None, history_filters=None):
    """The historical interval is B minus A. Storage costs start at day zero."""
    days_a, days_b = option_a.get("delay_days"), option_b.get("delay_days")
    if (type(days_a) is not int or type(days_b) is not int
            or not 1 <= days_a < days_b <= 365):
        return {"status": "INVALID_INPUT", "message": "보관일은 1~365일이며 B가 A보다 길어야 합니다."}
    if price_a is None:
        return {"status": "NEEDS_INPUT", "message": "A안의 가정 단가를 입력해 주세요."}
    try:
        if decimal(price_a, "A안 단가") <= 0:
            raise ValueError("A안 단가는 0보다 커야 합니다.")
    except ValueError as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}
    if history_filters is None and price_b is None:
        return {"status": "NEEDS_INPUT", "message": "B안의 가정 단가를 직접 입력해 주세요."}
    try:
        start = date.fromisoformat(start_date)
        sale_a = (start + timedelta(days=days_a)).isoformat()
        sale_b = (start + timedelta(days=days_b)).isoformat()
    except (ValueError, TypeError, OverflowError):
        return {"status": "INVALID_INPUT", "message": "보관 시작일을 확인해 주세요."}

    def template(option, name, price):
        return {"id": name, "delay_days": option["delay_days"],
                "loss_percent": option.get("loss_percent"), "daily_storage_cost": option.get("daily_storage_cost"),
                "future_price_per_kg": price}

    # The engine requires a reference price; only its future-option amounts are used.
    common = {"quantity_kg": quantity, "price_basis": price_basis, "current_price_per_kg": price_a,
              "current_fee_percent": option_a.get("fee_percent"), "cost_policy": "itemized",
              "included_costs": ["commission"] if price_basis == "commission_net" else [], "current_costs": []}
    a_template = template(option_a, "option_a", price_a)
    evidence = {"option_a": {"summary": "A안 단가 · 사용자 입력 가정", "sale_date": sale_a}}
    cautions = []
    if history_filters is None:
        b_templates = [template(option_b, "manual_b", price_b)]
        evidence["manual_b"] = {"summary": "B안 단가 · 사용자 입력 가정", "sale_date": sale_b}
    else:
        from agents.jeonghyeon.comparison_input import build_future_pair_input

        before = verify_integrated_price_sources(PROJECT_ROOT)
        if before["status"] != "OK" or before.get("full_content_verified") is not True:
            return {"status": "DATA_ERROR", "message": "과거 가격 자료의 원천을 확인하지 못했습니다. B안 단가를 직접 입력할 수 있습니다."}
        cost_fields = ("delay_days", "loss_percent", "daily_storage_cost")
        converted = build_future_pair_input(
            {**history_filters, "option_a_sale_date": sale_a, "option_b_sale_date": sale_b,
             "option_a_price_per_kg": price_a},
            option_a={key: option_a.get(key) for key in cost_fields},
            option_b={key: option_b.get(key) for key in cost_fields},
        )
        after = verify_integrated_price_sources(PROJECT_ROOT)
        if (after["status"] != "OK" or after.get("full_content_verified") is not True
                or before["selected_market_sha256"] != after["selected_market_sha256"]
                or before["comparison_source_sha256"] != after["comparison_source_sha256"]):
            return {"status": "DATA_ERROR", "message": "조회 중 자료가 바뀌었습니다. B안 단가를 직접 입력할 수 있습니다."}
        if converted["status"] != "OK":
            return converted
        converted_input = converted["comparison_input"]
        a_template = converted_input["option_a"]
        b_templates = converted_input["options_b"]
        evidence = converted["evidence"]
        cautions = converted["cautions"]

    a_input = _engine_option(option_a, a_template)
    result_a = compare_shipment_options({**common, "options": [a_input]})
    if result_a["status"] != "OK":
        return result_a
    options_b = [_engine_option(option_b, item) for item in b_templates]
    result_b = compare_shipment_options({**common, "options": options_b})
    if result_b["status"] != "OK":
        return result_b
    a_result = result_a["options"][0]
    a_amount = Decimal(a_result["hypothetical_proceeds_krw"])
    fee_b = decimal(option_b.get("fee_percent") or 0, "B안 수수료") / 100
    remaining_b = Decimal(result_b["options"][0]["remaining_kg"])
    cost_b = Decimal(result_b["options"][0]["total_cost_krw"])
    threshold = max(Decimal(0), (a_amount + cost_b) / (remaining_b * (1 - fee_b)))

    def row(result, option, other_cost, sale_date):
        def number(value):
            return fixed(Decimal(str(value)).normalize()) if value is not None else None

        return {"id": result["id"], "sale_date": sale_date, "delay_days": result["delay_days"],
                "loss_percent": number(option["loss_percent"]), "daily_storage_cost": number(option["daily_storage_cost"]),
                "other_cost_krw": number(other_cost), "fee_percent": number(option.get("fee_percent")),
                "remaining_kg": result["remaining_kg"], "total_cost_krw": result["total_cost_krw"],
                "price_per_kg": number(option["future_price_per_kg"]),
                "proceeds_krw": result["hypothetical_proceeds_krw"]}

    return {"status": "OK", "analysis_type": "future_pair", "quantity_kg": quantity, "price_basis": price_basis,
            "start_date": start_date, "interval_days": days_b - days_a, "evidence": evidence,
            "option_a": row(a_result, a_input, option_a["other_cost_krw"], sale_a),
            "options_b": [{**row(result, option, option_b["other_cost_krw"], sale_b),
                           "difference_vs_a_krw": fixed(Decimal(result["hypothetical_proceeds_krw"]) - a_amount)}
                          for result, option in zip(result_b["options"], options_b)],
            "break_even_vs_a_krw_per_kg": fixed(threshold),
            "minimum_whole_krw_per_kg": str(threshold.to_integral_value(rounding=ROUND_CEILING)),
            "warnings": [*result_b["warnings"], *cautions]}
