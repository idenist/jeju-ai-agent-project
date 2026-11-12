"""UI input snapshots and human-readable conditions; no inference or arithmetic."""
from __future__ import annotations

from copy import deepcopy

COST_LABELS = {"수수료": "commission", "선별비": "sorting", "포장비": "packaging", "운송비": "transport", "보관비": "storage", "기타": "other"}
INPUT_CONTEXTS = ["실제값·가정 구분 미확인", "사용자가 실제 조건을 바탕으로 입력", "가상 예시·사용자 가정"]
FIELD_LABELS = {"quantity": "물량", "price": "현재 단가", "basis": "가격 기준", "current_fee": "현재 수수료율",
                "included": "단가에 반영된 비용", "included_confirmed": "포함 비용 확인", "policy": "기타 비용 처리",
                "option_count": "보관 대안 수", "storage_available": "저장 가능 여부",
                "shipping_flexible": "출하일 변경 가능 여부", "input_context": "입력 구분"}
OPTION_LABELS = {"days": "보관기간", "loss": "기간 전체 감모율", "storage": "하루 보관비",
                 "future": "미래 단가 가정", "fee": "수수료율", "period": "기간 감모 확인"}


def form_snapshot(values) -> dict:
    keys = list(FIELD_LABELS)
    keys += ["current_" + category for category in COST_LABELS.values() if category != "commission"] + ["current_none"]
    for i in range(values.get("option_count", 1)):
        keys += [f"{field}_{i}" for field in OPTION_LABELS]
        keys += [f"option_{i}_{category}" for category in COST_LABELS.values() if category != "commission"] + [f"option_{i}_none"]
    result = {}
    for key in keys:
        value = values.get(key)
        result[key] = value.strip() if isinstance(value, str) else deepcopy(value)
    if result.get("included") is not None:
        result["included"] = sorted(result["included"])
    return result


def changed_labels(before: dict | None, after: dict) -> list[str]:
    if before is None:
        return []
    labels = []
    for key in dict.fromkeys([*before, *after]):
        if before.get(key) == after.get(key):
            continue
        label = FIELD_LABELS.get(key)
        if not label:
            parts = key.split("_")
            if parts[0] in OPTION_LABELS and parts[-1].isdigit():
                label = f"대안 {int(parts[-1]) + 1} {OPTION_LABELS[parts[0]]}"
            else:
                label = "현재 별도 비용" if key.startswith("current_") else "대안 별도 비용"
        if label not in labels:
            labels.append(label)
    return labels


def manual_scenario(values) -> dict:
    def value(key):
        raw = values.get(key)
        return (raw.strip() or None) if isinstance(raw, str) else raw

    def costs(prefix):
        entries = [{"category": category, "amount_krw": value(f"{prefix}_{category}")}
                   for category in COST_LABELS.values() if category != "commission" and value(f"{prefix}_{category}") is not None]
        return entries if entries or values.get(prefix + "_none", False) else None

    policy = {"보관비 외 기타 고정비용은 양쪽 동일하여 제외": "equal_excluded", "대안별 비용을 항목별 입력": "itemized"}.get(values.get("policy"))
    flags = {"예": True, "아니오": False, "미확인": None}
    result = {"quantity_kg": value("quantity"), "current_price_per_kg": value("price"),
              "price_basis": {"수수료 차감 후 단가": "commission_net", "수수료 차감 전 단가": "gross"}.get(values.get("basis")),
              "cost_policy": policy, "included_costs": [COST_LABELS[x] for x in values.get("included", [])] if values.get("included_confirmed") else None,
              "current_fee_percent": value("current_fee"), "current_costs": costs("current") if policy == "itemized" else None,
              "storage_available": flags.get(values.get("storage_available")),
              "shipping_flexible": flags.get(values.get("shipping_flexible")), "options": []}
    for i in range(values.get("option_count", 1)):
        confirmed = values.get(f"period_{i}", False)
        result["options"].append({"id": f"대안 {i + 1}", "delay_days": value(f"days_{i}"),
            "loss_percent": value(f"loss_{i}") if confirmed else None,
            "daily_storage_cost": value(f"storage_{i}"), "future_price_per_kg": value(f"future_{i}"),
            "fee_percent": value(f"fee_{i}"), "costs": costs(f"option_{i}") if policy == "itemized" else None,
            "loss_confirmed_for_period": confirmed})
    return result


def condition_rows(scenario: dict, context=INPUT_CONTEXTS[0]) -> list[dict]:
    """An entered value is not evidence that it is a real-world verified fact."""
    rows = []
    def add(label, value, unit="", status=None):
        missing = value is None or value == ""
        rows.append({"조건": label, "값": "미확인" if missing else f"{value}{unit}",
                     "구분": "미확인" if missing else status or "입력됨 · 실제 여부 미검증"})
    add("물량", scenario.get("quantity_kg"), "kg")
    add("현재 단가", scenario.get("current_price_per_kg"), "원/kg")
    add("가격 기준", {"commission_net": "수수료 차감 후", "gross": "수수료 차감 전"}.get(scenario.get("price_basis")))
    included = scenario.get("included_costs")
    names = {v: k for k, v in COST_LABELS.items()}
    add("반영 비용", None if included is None else (", ".join(names.get(x, x) for x in included) or "없음으로 확인"))
    add("기타 비용 처리", {"equal_excluded": "보관비 외 비용 동일 제외", "itemized": "대안별 항목 입력"}.get(scenario.get("cost_policy")))
    if scenario.get("price_basis") == "gross" or scenario.get("current_fee_percent") is not None:
        add("현재 수수료율", scenario.get("current_fee_percent"), "%")
    def add_costs(label, items):
        add(label, None if items is None else (", ".join(f"{names.get(x['category'], x['category'])} {x['amount_krw']}원" for x in items) or "별도 비용 없음으로 확인"))
    if scenario.get("cost_policy") == "itemized":
        add_costs("현재 별도 비용", scenario.get("current_costs"))
    for option in scenario.get("options", []):
        prefix = str(option.get("id", "보관 대안")) + " · "
        add(prefix + "기간", option.get("delay_days"), "일")
        add(prefix + "기간 전체 감모", option.get("loss_percent"), "%", "사용자 조건·가정 · 실제 감모 미검증")
        add(prefix + "하루 보관비", option.get("daily_storage_cost"), "원")
        add(prefix + "미래 단가", option.get("future_price_per_kg"), "원/kg", "사용자 가정 · 예측 아님")
        if scenario.get("price_basis") == "gross" or option.get("fee_percent") is not None:
            add(prefix + "수수료율", option.get("fee_percent"), "%")
        if scenario.get("cost_policy") == "itemized":
            add_costs(prefix + "별도 비용", option.get("costs"))
    for key, label in [("storage_available", "저장 가능"), ("shipping_flexible", "출하일 변경 가능")]:
        flag = scenario.get(key)
        add(label, "예" if flag is True else "아니오" if flag is False else None, status="사용자 응답 · 현장 미검증")
    if context == INPUT_CONTEXTS[2]:
        for row in rows:
            if row["구분"].startswith("입력됨"):
                row["구분"] = "가상 예시·사용자 가정"
    return rows
