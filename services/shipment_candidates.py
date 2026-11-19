"""Compare fixed shipment dates with one set of common financial assumptions."""
from datetime import date
from decimal import Decimal, ROUND_CEILING

from services.calculation_service import compare_shipment_options
from services.economics import decimal, fixed, minimum_price_for_proceeds


def compare_shipment_candidates(dates, *, excluded_dates, start, quantity, basis,
                                daily_storage, other_cost, fee, by_date, reference_date=None,
                                allow_unknown_losses=False):
    eligible = sorted(set(dates) - set(excluded_dates))
    if not 2 <= len(eligible) <= 3:
        return {"status": "NEEDS_INPUT", "message": "출하 가능한 후보일을 두세 개 알려주세요."}
    fields = ((start, "보관 시작일"), (quantity, "비교할 물량"), (basis, "단가의 수수료 기준"),
              (daily_storage, "하루 보관비"), (other_cost, "별도 비용"))
    if basis == "gross":
        fields += ((fee, "판매 수수료율"),)
    if missing := next((label for value, label in fields if value is None), None):
        return {"status": "NEEDS_INPUT", "message": f"{missing}부터 확인해 주세요."}
    try:
        if not isinstance(start, date) or basis not in {"gross", "commission_net"}:
            raise ValueError("보관 시작일과 단가 기준을 확인해 주세요.")
        if basis == "commission_net" and fee is not None and decimal(fee, "수수료율") != 0:
            raise ValueError("수수료 차감 후 단가에는 수수료율을 따로 입력하지 않아도 됩니다.")
        inputs = {}
        unconfirmed = {}
        for ship in eligible:
            label = f"{ship.month}월 {ship.day}일"
            days = (ship - start).days
            if not 0 <= days <= 365:
                raise ValueError(f"{label}이 보관 시작일보다 이르거나 보관 가능 범위를 넘습니다.")
            quote, loss = by_date.get(ship, (None, None))
            price = decimal(quote, "출하 단가") if quote is not None else None
            if price is not None and price <= 0:
                raise ValueError(f"{label}의 단가는 0원보다 커야 합니다.")
            if days and loss is None:
                if not allow_unknown_losses:
                    return {"status": "NEEDS_INPUT", "message": f"{label}의 기간 전체 감모율을 확인해 주세요."}
                unconfirmed[ship] = (days, price)
                continue
            if not days and loss is not None and decimal(loss, "기간 전체 감모율") != 0:
                raise ValueError(f"{label}은 보관 없이 출하하므로 보관기간 감모가 0%여야 합니다.")
            inputs[ship] = {"id": ship.isoformat(), "delay_days": days, "future_price_per_kg": price,
                            "loss_percent": loss if days else 0, "daily_storage_cost": daily_storage,
                            "fee_percent": fee if basis == "gross" else None,
                            "costs": [{"category": "other", "amount_krw": other_cost}]}
        if len(inputs) < 2:
            return {"status": "NEEDS_INPUT", "message": "비교금액이나 필요 단가를 계산할 수 있는 날짜가 두 곳은 있어야 해요."}
        earliest = next(iter(inputs.values()))
        reference = inputs.get(reference_date)
        if reference is None or reference["future_price_per_kg"] is None:
            reference = next((option for option in inputs.values() if option["future_price_per_kg"] is not None), None)
        if reference is None:
            return {"status": "NEEDS_INPUT", "message": "기준으로 삼을 후보일 한 곳의 kg당 단가를 입력해 주세요."}
        priced = compare_shipment_options({
            "quantity_kg": quantity, "price_basis": basis, "current_price_per_kg": reference["future_price_per_kg"],
            "current_fee_percent": earliest["fee_percent"], "cost_policy": "itemized", "current_costs": earliest["costs"],
            "included_costs": ["commission"] if basis == "commission_net" else [],
            "options": [option for option in inputs.values() if option["delay_days"]],
        })
        if priced["status"] != "OK":
            return priced
        outputs = {row["id"]: row for row in priced["options"]}
        if not earliest["delay_days"]:
            outputs[earliest["id"]] = {"remaining_kg": str(quantity), "storage_cost_krw": "0",
                                      "total_cost_krw": str(other_cost),
                                      "hypothetical_proceeds_krw": (priced["current_proceeds_krw"]
                                                                   if earliest is reference else None)}
        baseline = Decimal(outputs[reference["id"]]["hypothetical_proceeds_krw"])
        first_amount = outputs.get(eligible[0].isoformat(), {}).get("hypothetical_proceeds_krw")
        net_rate = 1 - decimal(fee, "수수료율") / 100 if basis == "gross" else Decimal(1)
        rows = []
        for ship in eligible:
            if ship in unconfirmed:
                days, price = unconfirmed[ship]
                rows.append({"sale_date": ship.isoformat(), "delay_days": days,
                             "price_per_kg": fixed(price) if price is not None else None,
                             "loss_percent": None, "remaining_kg": None,
                             "storage_cost_krw": fixed(decimal(daily_storage, "하루 보관비") * days),
                             "proceeds_krw": None, "difference_vs_first_krw": None,
                             "difference_vs_reference_krw": None, "break_even_vs_reference_krw_per_kg": None,
                             "minimum_whole_krw_per_kg": None, "unconfirmed": ["loss_percent"]})
                continue
            option = inputs[ship]
            row = outputs[option["id"]]
            price = option["future_price_per_kg"]
            amount = row["hypothetical_proceeds_krw"]
            proceeds = Decimal(amount) if amount is not None else None
            threshold = (minimum_price_for_proceeds(baseline, Decimal(row["total_cost_krw"]),
                                                    Decimal(row["remaining_kg"]) * net_rate)
                         if proceeds is None else None)
            rows.append({"sale_date": option["id"], "delay_days": option["delay_days"],
                         "price_per_kg": fixed(price) if price is not None else None,
                         "loss_percent": str(option["loss_percent"]),
                         "remaining_kg": row["remaining_kg"], "storage_cost_krw": row["storage_cost_krw"],
                         "proceeds_krw": amount,
                         "difference_vs_first_krw": (fixed(proceeds - Decimal(first_amount))
                                                     if proceeds is not None and first_amount is not None else None),
                         "difference_vs_reference_krw": fixed(proceeds - baseline) if proceeds is not None else None,
                         "break_even_vs_reference_krw_per_kg": fixed(threshold) if threshold is not None else None,
                         "minimum_whole_krw_per_kg": (str(threshold.to_integral_value(rounding=ROUND_CEILING))
                                                      if threshold is not None else None)})
        quoted = [row for row in rows if row["proceeds_krw"] is not None]
        highest = max(Decimal(row["proceeds_krw"]) for row in quoted)
        return {"status": "OK", "analysis_type": "shipment_candidates", "start_date": start.isoformat(),
                "quantity_kg": str(quantity), "price_basis": basis, "daily_storage_cost_krw": str(daily_storage),
                "other_cost_krw": str(other_cost), "fee_percent": str(fee) if basis == "gross" else None,
                "rows": rows, "excluded_dates": [day.isoformat() for day in sorted(set(dates) - set(eligible))],
                "reference_date": reference["id"], "reference_proceeds_krw": fixed(baseline),
                "ranking_complete": len(quoted) == len(rows),
                "highest_dates": [row["sale_date"] for row in quoted if Decimal(row["proceeds_krw"]) == highest],
                "is_future_forecast": False, "warnings": priced["warnings"]}
    except (ValueError, TypeError, ArithmeticError) as error:
        return {"status": "INVALID_INPUT", "message": str(error)}
