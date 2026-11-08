"""Strict function schemas and a whitelist of application-side tools."""
from __future__ import annotations

from copy import deepcopy
from .comparison import compare_shipment_options
from .state import ScenarioStore


def object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def nullable(kind: str, **kwargs) -> dict:
    return {"type": [kind, "null"], **kwargs}


COST = object_schema({"category": {"type": "string", "enum": ["sorting", "packaging", "transport", "storage", "other"]},
                      "amount_krw": {"type": "number", "minimum": 0}})
OPTION = object_schema({"id": {"type": "string"}, "delay_days": nullable("integer", minimum=1, maximum=365,
                        description="지연 보관일만 입력. 즉시 출하 0일은 별도 기준이므로 대안 배열에 넣지 않습니다."),
                        "loss_percent": nullable("number", minimum=0, exclusiveMaximum=100), "daily_storage_cost": nullable("number", minimum=0),
                        "fee_percent": nullable("number", minimum=0, exclusiveMaximum=100), "future_price_per_kg": nullable("number", minimum=0),
                        "costs": nullable("array", items=COST),
                        "loss_confirmed_for_period": {"type": "boolean"}})
PATCH = object_schema({"quantity_kg": nullable("number"), "current_price_per_kg": nullable("number"),
                       "price_basis": nullable("string", enum=["commission_net", "gross", None]),
                       "current_fee_percent": nullable("number"),
                       "cost_policy": nullable("string", enum=["equal_excluded", "itemized", None]),
                       "included_costs": nullable("array", items={"type": "string", "enum": ["commission", "sorting", "packaging", "transport", "storage", "other"]}),
                       "current_costs": nullable("array", items=COST),
                       "storage_available": nullable("boolean"), "shipping_flexible": nullable("boolean"),
                       "options": nullable("array", items=OPTION, maxItems=3)})


def tool(name: str, description: str, properties: dict) -> dict:
    properties = {**properties, "finish_turn": {"type": "boolean", "description":
        "이번 질문의 자료 조회·조건 반영을 모두 마쳤으면 true. 추가 도구가 필요할 때만 false. true로 조건 저장하면 Python이 즉시 비교하거나 부족한 조건을 안내하므로 별도 비교·최종 문장 호출 불필요."}}
    return {"type": "function", "name": name, "description": description,
            "strict": True, "parameters": object_schema(properties)}


TOOLS = [
    tool("update_scenario", "사용자가 제공·확인한 조건만 저장. patch의 null은 기존값 유지. 대안은 전체 목록 교체. 기간이 바뀌면 새 기간 감모를 사용자가 확인했을 때만 loss_confirmed_for_period=true.",
         {"expected_version": {"type": "integer"}, "patch": PATCH,
          "clear_fields": {"type": "array", "items": {"type": "string", "enum": ["quantity_kg", "current_price_per_kg", "price_basis", "current_fee_percent", "cost_policy", "included_costs", "current_costs", "storage_available", "shipping_flexible"]}}}),
    tool("compare_shipment_options", "저장된 현재 조건으로 대안별 금액과 필요한 미래 단가 계산. 입력이 부족하면 확인할 항목 반환. 미래가격 예측·최선 출하일 추천 아님.",
         {"expected_version": {"type": "integer"}}),
    tool("compare_sensitivity", "사용자가 직접 정한 기간 전체 감모율·미래가격 목록의 조건부 민감도 표. 기존 보관기간·비용은 고정하고 원래 조건을 바꾸지 않음. 값을 만들거나 확률·가격예측으로 표시하지 말 것. 목록이 없으면 사용자에게 먼저 질문할 것.",
         {"expected_version": {"type": "integer"}, "option_id": {"type": "string"},
          "loss_values": nullable("array", minItems=1, maxItems=7, items={"type": "number", "minimum": 0, "exclusiveMaximum": 100}),
          "price_values": nullable("array", minItems=1, maxItems=7, items={"type": "number", "minimum": 0})}),
    tool("get_historical_shipment_context", "제주데이터허브 #855의 실제 과거 월별 출하량 조회. 경매 품종을 출하 품종으로 자동 매핑하지 말 것. 2026 실시간 공급 아님.",
         {"shipment_variety": {"type": "string"}, "target_month": {"type": "integer", "minimum": 1, "maximum": 12},
          "start_year": nullable("integer"), "end_year": nullable("integer")}),
    tool("get_market_evidence", "가락시장 확보 CSV의 동일 거래조건 경락가격 참고. 농가 실수취가·실시간 시세가 아님. 조건이 없으면 null로 질문할 항목 확인.",
         {"market_code": nullable("string"), "variety": nullable("string"), "grade": nullable("string"),
          "size": nullable("string"), "package": nullable("string"), "package_kg": nullable("number"),
          "as_of": nullable("string")}),
    tool("explain_term", "가격기준·감모·비용·자료 범위에 대한 검증된 사용 안내를 표시합니다. 용어 질문에는 계산 조건을 요구하지 마세요.",
         {"topic": {"type": "string", "enum": ["price_basis", "loss_rate", "cost_policy", "data_scope", "forecast_limits"]}}),
]

HELP = {
    "price_basis": "수수료 차감 후 단가는 판매수수료를 이미 뺀 가격입니다. 수수료를 다시 차감하지 않습니다. 선별·포장·운송·보관비까지 포함됐는지는 따로 확인해야 합니다. 차감 전 단가라면 수수료율을 별도로 입력합니다.",
    "loss_rate": "기간 전체 감모율은 보관을 마친 뒤 판매할 수 없게 줄어드는 물량의 비율입니다. 하루 비율과 다릅니다. 보관기간을 바꾸면 새 기간의 감모를 다시 확인해야 하며, 모르는 값은 실제 값처럼 채우지 않습니다.",
    "cost_policy": "보관비 외 고정비가 양쪽에서 동일하다고 확인한 경우에만 비교에서 제외합니다. 다른 비용은 항목별로 입력하고 단가에 이미 포함된 비용은 다시 빼지 않습니다. 빈칸은 비용 없음이 아니라 미확인입니다.",
    "data_scope": "시장자료는 실제 관측일의 경락가격이며 농가 실수취 단가가 아닙니다. 제주 출하통계는 과거 신고 출하량입니다. 조회 결과의 출처·기간·품종·단위를 확인하고 서로 다른 분류나 기간을 자동 연결하지 않습니다.",
    "forecast_limits": "현재 확보자료만으로 미래가격이나 최적 출하일을 확정하지 않습니다. 사용자 조건에서 기다리려면 필요한 최소 단가를 계산하거나, 사용자가 정한 미래 단가 가정에서 금액을 비교할 수 있습니다.",
}


def explain_term(topic: str) -> dict:
    if not isinstance(topic, str) or topic not in HELP:
        return {"status": "INVALID_INPUT", "message": "안내 주제를 확인하세요."}
    return {"status": "OK", "topic": topic, "explanation": HELP[topic]}


def execute_tool(name: str, args: dict, store: ScenarioStore) -> dict:
    if not isinstance(args, dict):
        return {"status": "INVALID_INPUT", "message": "도구 입력은 객체여야 합니다."}
    schemas = {t["name"]: t["parameters"] for t in TOOLS}
    if name not in schemas:
        return {"status": "INVALID_INPUT", "message": "허용되지 않은 도구입니다."}
    if set(args) - set(schemas[name]["properties"]):
        return {"status": "INVALID_INPUT", "message": "허용되지 않은 도구 인수입니다."}
    args = deepcopy(args)
    finish = args.pop("finish_turn", False)
    if type(finish) is not bool:
        return {"status": "INVALID_INPUT", "message": "완료 표시는 true 또는 false여야 합니다."}
    try:
        if name == "update_scenario":
            return store.update(args.get("patch"), args.get("expected_version"), args.get("clear_fields"))
        if name == "explain_term":
            return explain_term(args.get("topic"))
        if name in {"compare_shipment_options", "compare_sensitivity"}:
            version = args.get("expected_version")
            if type(version) is not int or version != store.version:
                return {"status": "INVALID_INPUT", "message": "계산할 시나리오 버전을 확인하세요.", "current": store.snapshot()}
            if name == "compare_sensitivity":
                from .sensitivity import compare_sensitivity
                result = compare_sensitivity(store.scenario, args.get("option_id"), args.get("loss_values"), args.get("price_values"))
            else:
                result = compare_shipment_options(store.scenario)
            return {**result, "scenario_version": store.version}
        from .evidence import get_historical_shipment_context, get_market_evidence
        return (get_historical_shipment_context if name == "get_historical_shipment_context" else get_market_evidence)(deepcopy(args))
    except (KeyError, TypeError, ValueError):
        return {"status": "INVALID_INPUT", "message": "도구 입력 형식을 확인하세요."}
    except (OSError, ArithmeticError):
        return {"status": "DATA_ERROR", "message": "자료 파일과 계산 입력을 확인하세요."}
