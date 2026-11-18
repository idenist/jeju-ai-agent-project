"""Compare two future shipments using the existing option calculator."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
from time import monotonic

from services.calculation_service import compare_shipment_options
from services.data_contract import PAIR_FILES, PRICE_FILE, REPORT_FILE, verify_cached_price_sources
from services.data_refresh import digest, resolve_market_root
from services.task_context import data_revision
from services.economics import decimal, fixed
from services.price_data_gap import classify_price_gap, price_gap
from core.config import PROJECT_ROOT
from core.latency import log_latency


OPTION_FIELDS = ("delay_days", "loss_percent", "daily_storage_cost", "other_cost_krw", "fee_percent")
MAX_RATE_PERCENT = 99.99
INPUT_FIELDS = ("quantity", "price_basis", "start_date", "price_a", "price_b", "history_filters")
LABELS = {"quantity": "비교 물량(kg)", "price_basis": "수수료 차감 전·후 기준",
          "start_date": "보관 시작일", "price_a": "A안 가정 단가(원/kg)", "price_b": "B안 가정 단가(원/kg)"}
OPTION_LABELS = {"delay_days": "보관일", "loss_percent": "기간 전체 감모율(%)",
                 "daily_storage_cost": "전체 물량 하루 보관비(원)", "other_cost_krw": "별도 비용(원)",
                 "fee_percent": "수수료율(%)"}
HISTORY_FIELDS = {"variety": "품종", "grade": "등급", "size": "크기", "package": "포장 종류",
                  "package_kg": "포장 중량(kg)", "market_code": "시장코드", "trade_type": "거래방식"}


def price_revision():
    try:
        active_price = resolve_market_root(PROJECT_ROOT) / PRICE_FILE
        paths = dict.fromkeys([active_price, *(PROJECT_ROOT / path for path in (PRICE_FILE, *PAIR_FILES, REPORT_FILE))])
        return (*data_revision(), *(digest(path) for path in paths))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def validate_future_pair_inputs(inputs):
    if inputs["price_basis"] not in (None, "gross", "commission_net"):
        raise ValueError("수수료 차감 전·후 단가 기준을 확인해 주세요.")
    start_date = inputs["start_date"]
    if start_date is not None:
        try:
            start = date.fromisoformat(start_date)
            inputs["start_date"] = start.isoformat()
        except (ValueError, TypeError):
            raise ValueError("보관 시작일을 연-월-일로 알려 주세요.") from None
    for field in ("quantity", "price_a", "price_b"):
        value = inputs[field]
        if value is None:
            continue
        number = decimal(value, LABELS[field])
        if number < 0 or (field != "price_b" and number == 0):
            raise ValueError(f"{LABELS[field]}의 범위를 확인해 주세요.")
    for key, side in (("option_a", "A"), ("option_b", "B")):
        for field, value in inputs[key].items():
            if value is None:
                continue
            label = f"{side}안 {OPTION_LABELS[field]}"
            if field == "delay_days":
                if type(value) is not int or not 1 <= value <= 365:
                    raise ValueError(f"{label}은 1~365일 사이 정수여야 합니다.")
                if start_date is not None and value > (date.max - start).days:
                    raise ValueError("보관 시작일과 보관일로 출하일을 계산할 수 없습니다.")
                continue
            number = decimal(value, label)
            if number < 0 or (field in {"loss_percent", "fee_percent"} and number > Decimal(str(MAX_RATE_PERCENT))):
                raise ValueError(f"{label}의 범위를 확인해 주세요.")
            if field == "fee_percent" and inputs["price_basis"] == "commission_net" and number != 0:
                raise ValueError("수수료 차감 후 단가와 별도 수수료율이 함께 입력됐습니다. 단가 기준을 확인해 주세요.")
    a, b = (inputs[side]["delay_days"] for side in ("option_a", "option_b"))
    if a is not None and b is not None and a >= b:
        raise ValueError("B안 보관일은 A안보다 길어야 합니다.")


def _engine_option(option, template):
    costs = option.get("other_cost_krw")
    return {**template, "fee_percent": option.get("fee_percent"),
            "costs": None if costs is None else [{"category": "other", "amount_krw": costs}]}


def _verified_history(lookup, args, **kwargs):
    file_gap = {"status": "DATA_ERROR", "data_gap": price_gap("FILE_UNVERIFIED", comparison=True),
                "requested_conditions": {field: args[field] for field in HISTORY_FIELDS if field in args},
                "source_verification": {"status": "DATA_ERROR"}}
    started = monotonic()
    before = verify_cached_price_sources(PROJECT_ROOT)
    log_latency("csv_verify_before", started)
    if before["status"] != "OK" or before.get("full_content_verified") is not True:
        return file_gap
    started = monotonic()
    result = lookup(args, **kwargs)
    log_latency("csv_lookup", started)
    started = monotonic()
    after = verify_cached_price_sources(PROJECT_ROOT)
    log_latency("csv_verify_after", started)
    if (after["status"] != "OK" or after.get("full_content_verified") is not True
            or before["source_fingerprint"] != after["source_fingerprint"]):
        return file_gap
    if code := classify_price_gap(result):
        return {**result, "data_gap": price_gap(code, comparison=True)}
    return result


def preview_future_pair_history(start_date, days_a, days_b, price_a, filters):
    """Read historical evidence before the cost inputs are known."""
    from services.price_scenario import execute_scenario_tool

    start = date.fromisoformat(start_date)
    result = _verified_history(execute_scenario_tool, {
        **filters, "option_a_sale_date": (start + timedelta(days=days_a)).isoformat(),
        "option_b_sale_date": (start + timedelta(days=days_b)).isoformat(),
        "option_a_price_per_kg": price_a,
    })
    if result.get("manual_price_allowed"):
        result = {**result, "manual_price_required": True}
    return result


def compare_future_pair_threshold_reference(*, quantity, price_basis, start_date,
                                            option_a, option_b, price_a):
    """Return a conditional break-even reference without inventing unknown costs.

    The displayed threshold sets the *net difference* between unconfirmed costs
    to zero. Known costs are included, while missing costs remain explicit and
    their effect is shown as a sensitivity per 10,000 KRW.
    """
    days_a, days_b = option_a.get("delay_days"), option_b.get("delay_days")
    if (type(days_a) is not int or type(days_b) is not int
            or not 1 <= days_a < days_b <= 365):
        return {"status": "INVALID_INPUT", "message": "보관일은 1~365일이며 B가 A보다 길어야 합니다."}
    if price_basis not in {"gross", "commission_net"}:
        return {"status": "NEEDS_INPUT", "message": "단가의 수수료 기준을 확인해 주세요."}
    try:
        q = decimal(quantity, "비교 물량")
        pa = decimal(price_a, "A안 단가")
        loss_a = decimal(option_a.get("loss_percent"), "A안 감모율") / 100
        loss_b = decimal(option_b.get("loss_percent"), "B안 감모율") / 100
        if q <= 0 or pa <= 0:
            raise ValueError("비교 물량과 A안 단가는 0보다 커야 합니다.")
        fee_a = fee_b = Decimal(0)
        if price_basis == "gross":
            fee_a = decimal(option_a.get("fee_percent"), "A안 수수료") / 100
            fee_b = decimal(option_b.get("fee_percent"), "B안 수수료") / 100
        start = date.fromisoformat(start_date)
    except (ValueError, TypeError, ArithmeticError) as exc:
        return {"status": "NEEDS_INPUT", "message": str(exc)}

    def known_cost(option, days):
        total = Decimal(0)
        missing = []
        for field, label, multiplier in (
            ("daily_storage_cost", "하루 보관비", days),
            ("other_cost_krw", "별도 비용", 1),
        ):
            value = option.get(field)
            if value is None:
                missing.append(label)
            else:
                total += decimal(value, label) * multiplier
        return total, missing

    cost_a, missing_a = known_cost(option_a, days_a)
    cost_b, missing_b = known_cost(option_b, days_b)
    sale_a = (start + timedelta(days=days_a)).isoformat()
    sale_b = (start + timedelta(days=days_b)).isoformat()
    remaining_a = q * (1 - loss_a)
    remaining_b = q * (1 - loss_b)
    denominator = remaining_b * (1 - fee_b)
    if denominator <= 0:
        return {"status": "INVALID_INPUT", "message": "B안 판매 가능 물량 또는 수수료를 확인해 주세요."}
    a_reference_amount = remaining_a * pa * (1 - fee_a) - cost_a
    threshold = max(Decimal(0), (a_reference_amount + cost_b) / denominator)
    missing_fields = [*(f"A안 {name}" for name in missing_a), *(f"B안 {name}" for name in missing_b)]

    def number(value):
        # 사용자가 넣은 비용은 int/float로 들어올 수 있어 Decimal로 바꾼 뒤 표시한다(0원도 그대로 유지).
        return fixed(Decimal(str(value)).normalize())

    def optional_number(option, field, label):
        """Serialize the same user value used by Decimal cost calculations."""
        value = option.get(field)
        return number(decimal(value, label)) if value is not None else None

    def row(name, days, sale_date, loss, fee, option, remaining, price, ref_amount=None):
        return {
            "id": name, "sale_date": sale_date, "delay_days": days,
            "loss_percent": number(loss * 100),
            "daily_storage_cost": optional_number(option, "daily_storage_cost", "하루 보관비"),
            "other_cost_krw": optional_number(option, "other_cost_krw", "별도 비용"),
            "fee_percent": number(fee * 100) if price_basis == "gross" else None,
            "remaining_kg": number(remaining), "price_per_kg": number(price) if price is not None else None,
            "proceeds_krw": None, "difference_vs_a_krw": None,
            "known_cost_krw": number(cost_a if name == "option_a" else cost_b),
            "reference_amount_krw": number(ref_amount) if ref_amount is not None else None,
        }

    return {
        "status": "OK", "analysis_type": "future_pair", "threshold_only": True,
        "quantity_kg": number(q), "price_basis": price_basis, "start_date": start_date,
        "interval_days": days_b - days_a,
        "option_a": row("option_a", days_a, sale_a, loss_a, fee_a, option_a, remaining_a, pa,
                         a_reference_amount),
        "options_b": [row("manual_b", days_b, sale_b, loss_b, fee_b, option_b, remaining_b, None)],
        "break_even_vs_a_krw_per_kg": number(threshold),
        "minimum_whole_krw_per_kg": str(threshold.to_integral_value(rounding=ROUND_CEILING)),
        "a_reference_amount_krw": number(a_reference_amount),
        "unknown_cost_fields": missing_fields,
        "unknown_cost_difference_definition": "B안 미확인 비용 합계 - A안 미확인 비용 합계",
        "unknown_cost_delta_per_10000_krw_per_kg": number(Decimal(10000) / denominator),
        "threshold_assumption": "미확인 비용 차이를 0원으로 놓은 참고 손익분기 가격",
        "evidence": {"option_a": {"summary": "A안 단가 · 사용자 입력 가정", "sale_date": sale_a},
                     "manual_b": {"summary": "B안 단가 미확인 · 과거 가격 자료 미조회", "sale_date": sale_b}},
        "historical_price_lookup": "not_used",
        "suppress_market_evidence": True,
        "is_future_forecast": False,
        "warnings": [
            "B안 단가가 없어 실제 수취액과 어느 안이 유리한지 판단하지 않았습니다.",
            "표시한 손익분기 가격은 미확인 비용 차이를 0원으로 둔 참고값입니다.",
            "미래가격 전망이나 실행 권고가 아닙니다.",
        ],
    }


def compare_future_pair_history_reference(*, quantity, price_basis, start_date,
                                          option_a, option_b, price_a, history_preview):
    """Join historical B-price scenarios to a conditional cost-aware threshold."""
    reference = compare_future_pair_threshold_reference(
        quantity=quantity, price_basis=price_basis, start_date=start_date,
        option_a=option_a, option_b=option_b, price_a=price_a,
    )
    if reference["status"] != "OK":
        return reference
    scenarios = history_preview.get("scenarios") if isinstance(history_preview, dict) else None
    if not scenarios:
        return {"status": "NO_DATA", "message": "과거 가격 시나리오를 확인할 수 없습니다."}

    b = reference["options_b"][0]
    remaining_b = Decimal(b["remaining_kg"])
    fee_b = Decimal(b["fee_percent"] or "0") / 100 if price_basis == "gross" else Decimal(0)
    known_cost_b = Decimal(b["known_cost_krw"])
    a_amount = Decimal(reference["a_reference_amount_krw"])
    rows = []
    for scenario in scenarios:
        price = scenario.get("option_b_price_per_kg")
        if price is None:
            return {"status": "NEEDS_INPUT", "message": "A안 가정 단가가 있어야 과거 가격 시나리오의 금액을 계산할 수 있습니다."}
        price_value = decimal(price, "과거 가격 시나리오")
        b_reference_amount = remaining_b * price_value * (1 - fee_b) - known_cost_b
        rows.append({
            "id": f"past_{scenario['basis']}_{history_preview['interval_days']}d",
            "sale_date": b["sale_date"], "delay_days": b["delay_days"],
            "loss_percent": b["loss_percent"], "fee_percent": b["fee_percent"],
            "daily_storage_cost": b["daily_storage_cost"], "other_cost_krw": b["other_cost_krw"],
            "basis": scenario["basis"], "label": scenario["label"],
            "change_pct": str(scenario["change_pct"]),
            "price_per_kg": fixed(price_value),
            "reference_amount_krw": fixed(b_reference_amount),
            "reference_difference_krw": fixed(b_reference_amount - a_amount),
            "known_cost_krw": b["known_cost_krw"],
            "remaining_kg": b["remaining_kg"],
        })
    from services.past_reach import add_past_outcomes, past_pairs_reaching

    reference.update({
        "history_scenarios": True,
        "past_reach": past_pairs_reaching(history_preview.get("change_pcts"), price_a,
                                          reference["break_even_vs_a_krw_per_kg"], history_preview["interval_days"]),
        "history_preview": {key: history_preview[key] for key in
                            ("interval_days", "clean_pairs", "warning_pairs", "unavailable_pairs",
                             "base_date_range", "conditions", "source")
                            if key in history_preview},
        "options_b": rows,
        "evidence": {
            "option_a": {"summary": "A안 단가 · 사용자 입력 가정", "sale_date": reference["option_a"]["sale_date"]},
            **{row["id"]: {"summary": f"B안 {row['price_per_kg']}원/kg · 과거 {row['change_pct']}% 변동 사례 가정",
                           "price_type": "historical_scenario"} for row in rows},
        },
        "historical_price_lookup": "used",
        "suppress_market_evidence": True,
        "warnings": [
            "과거 동일규격 가격변동 사례를 B안 가격 가정으로 적용했습니다. 미래가격 예측 확률이 아닙니다.",
            "표의 비교금액 차이는 미확인 비용 차이를 반영하기 전의 참고값이며 실제 유불리 판단이 아닙니다.",
            "미확인 비용 차이를 0원으로 둔 참고 손익분기 가격입니다.",
            # 사례가 10쌍 미만이면 참고 결과에도 같은 안내를 남긴다.
            *([history_preview["sample_note"]] if history_preview.get("sample_note") else []),
        ],
    })
    return add_past_outcomes(reference, history_preview.get("change_pcts") or ())


def compare_future_pair(*, quantity, price_basis, start_date, option_a, option_b,
                        price_a, price_b=None, history_filters=None, on_progress=None):
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
    cautions, change_pcts = [], None
    if history_filters is None:
        b_templates = [template(option_b, "manual_b", price_b)]
        evidence["manual_b"] = {"summary": "B안 단가 · 사용자 입력 가정", "sale_date": sale_b}
    else:
        from services.comparison_input import build_future_pair_input

        cost_fields = ("delay_days", "loss_percent", "daily_storage_cost")
        if on_progress:
            on_progress("price_history")
        converted = _verified_history(
            build_future_pair_input,
            {**history_filters, "option_a_sale_date": sale_a, "option_b_sale_date": sale_b,
             "option_a_price_per_kg": price_a},
            option_a={key: option_a.get(key) for key in cost_fields},
            option_b={key: option_b.get(key) for key in cost_fields},
        )
        if converted["status"] != "OK":
            return converted
        converted_input = converted["comparison_input"]
        a_template = converted_input["option_a"]
        b_templates = converted_input["options_b"]
        evidence = converted["evidence"]
        cautions = converted["cautions"]
        change_pcts = converted.get("change_pcts")

    from services.past_reach import add_past_outcomes, past_pairs_reaching

    a_input = _engine_option(option_a, a_template)
    if on_progress:
        on_progress("compare")
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

    def number(value):
        if value is None:
            return None
        text = fixed(Decimal(str(value)))
        return text.rstrip("0").rstrip(".") if "." in text else text

    def row(result, option, other_cost, sale_date):
        return {"id": result["id"], "sale_date": sale_date, "delay_days": result["delay_days"],
                "loss_percent": number(option["loss_percent"]), "daily_storage_cost": number(option["daily_storage_cost"]),
                "other_cost_krw": number(other_cost), "fee_percent": number(option.get("fee_percent")),
                "remaining_kg": number(result["remaining_kg"]), "total_cost_krw": number(result["total_cost_krw"]),
                "price_per_kg": number(option["future_price_per_kg"]),
                "proceeds_krw": number(result["hypothetical_proceeds_krw"])}

    result = {"status": "OK", "analysis_type": "future_pair", "quantity_kg": quantity, "price_basis": price_basis,
            "start_date": start_date, "interval_days": days_b - days_a, "evidence": evidence,
            "option_a": row(a_result, a_input, option_a["other_cost_krw"], sale_a),
            "options_b": [{**row(result, option, option_b["other_cost_krw"], sale_b),
                           "difference_vs_a_krw": fixed(Decimal(result["hypothetical_proceeds_krw"]) - a_amount)}
                          for result, option in zip(result_b["options"], options_b)],
            "break_even_vs_a_krw_per_kg": fixed(threshold),
            "minimum_whole_krw_per_kg": str(threshold.to_integral_value(rounding=ROUND_CEILING)),
            "past_reach": past_pairs_reaching(change_pcts, price_a, threshold, days_b - days_a),
            "historical_price_lookup": "used" if history_filters is not None else "not_used",
            "suppress_market_evidence": history_filters is None,
            "warnings": [*result_b["warnings"], *cautions]}
    return add_past_outcomes(result, change_pcts or ())
