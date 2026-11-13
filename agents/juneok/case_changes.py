"""Read-only saved/current comparison; archived outputs never establish facts."""
from copy import deepcopy
from decimal import Decimal
from .comparison import compare_shipment_options, fixed

NUMBERS = {"quantity_kg", "current_price_per_kg", "current_fee_percent", "delay_days", "loss_percent", "daily_storage_cost", "fee_percent", "future_price_per_kg", "package_kg", "amount_krw"}


def normalized(value, key=""):
    if value is None: return None
    if key in NUMBERS:
        number = Decimal(str(value))
        return "0" if number == 0 else str(number.normalize())
    if isinstance(value, dict): return {k: normalized(v, k) for k, v in value.items()}
    if isinstance(value, list):
        values = [normalized(v) for v in value]
        return sorted(values, key=str) if key in {"costs", "current_costs", "included_costs"} else values
    return value


def compare_cases(saved, current, current_context, revision, input_context):
    if saved is None:
        return {"status": "NEEDS_INPUT", "missing_fields": ["comparison_reference"], "message": "비교할 저장 JSON을 확인하고 변경 검토 기준으로 선택하세요."}
    old, new = saved["snapshot"]["scenario"], current["scenario"]
    changes = []
    def add(field, before, after, key=None):
        if normalized(before, key or field) != normalized(after, key or field):
            changes.append({"field": field, "before": deepcopy(before), "after": deepcopy(after)})
    for key in sorted(set(old) | set(new)):
        if key != "options": add(key, old.get(key), new.get(key))
    prior = {o["id"]: o for o in old["options"]}; present = {o["id"]: o for o in new["options"]}
    for key in sorted(set(prior) | set(present)):
        if key not in prior or key not in present:
            add("options." + key, prior.get(key), present.get(key))
        else:
            for field in sorted(set(prior[key]) | set(present[key])):
                if field not in {"id", "loss_confirmed_for_period"}:
                    add(f"options.{key}.{field}", prior[key].get(field), present[key].get(field), field)
    for key in ["shipment_query", "market_query", "sensitivity_query"]:
        add("task_context." + key, (saved.get("task_context") or {}).get(key), current_context.get(key))
    add("input_context", saved["input_context"], input_context)
    baseline = compare_shipment_options(old); latest = compare_shipment_options(new)
    comparable = all(normalized(old.get(k), k) == normalized(new.get(k), k) for k in ["price_basis", "included_costs", "cost_policy"])
    a = {o["id"]: o for o in baseline.get("options", [])}; b = {o["id"]: o for o in latest.get("options", [])}
    outcomes = []
    for key in sorted(set(a) | set(b)):
        x, y = a.get(key), b.get(key)
        outcomes.append({"option_id": key, "saved_minimum": x["minimum_whole_krw_per_kg"] if x else None,
                         "current_minimum": y["minimum_whole_krw_per_kg"] if y else None,
                         "difference_per_kg": fixed(Decimal(y["minimum_whole_krw_per_kg"]) - Decimal(x["minimum_whole_krw_per_kg"])) if x and y and comparable else None})
    return {"status": "OK", "analysis_type": "case_changes", "saved_name": saved["name"], "saved_at": saved["created_at_kst"],
            "review_status": "COMPLETE" if baseline["status"] == latest["status"] == "OK" else "PARTIAL",
            "scenario_version": current["version"], "saved_version": saved["snapshot"]["version"], "changes": changes,
            "data_changed": saved["data_revision"] != list(revision), "directly_comparable": comparable,
            "saved_recalculated": baseline, "current_recalculated": latest, "outcomes": outcomes,
            "archived_result_count": len(saved["archived_results"]),
            "warnings": ["저장 조건과 현재 조건을 현재 계산 규칙으로 다시 계산했습니다. 당시 보관 결과의 숫자를 현재 사실로 재사용하지 않습니다.",
                         "자료 버전 변경은 표시만 하며 과거 시장/출하 관측을 자동 재조회하지 않았습니다.",
                         "여러 입력이 바뀐 차이를 단일 원인의 효과나 실제 수익 개선으로 단정하지 않습니다. 가격/비용 기준이 다르면 단가 차이를 산출하지 않습니다.",
                         "파일 이름·입력·가정은 사용자가 제공한 정보이며 외부 사실 검증이 아닙니다."], "is_future_forecast": False}
