"""One read-only review task with explicit component statuses and provenance."""
from copy import deepcopy
from agents.common.calculation_service import compare_shipment_options
from .evidence import get_historical_shipment_context, get_market_evidence
from .insights import decision_conditions
from .sensitivity import compare_sensitivity


def build_review_brief(scenario, shipment_query=None, market_query=None, option_id=None, loss_values=None, price_values=None):
    if option_id is None and len(scenario.get("options", [])) == 1:
        option_id = scenario["options"][0]["id"]
    entries = [{"tool": "compare_shipment_options", "result": compare_shipment_options(scenario)}]
    if shipment_query is None:
        entries.append({"tool": "get_historical_shipment_context", "result": {
            "status": "NEEDS_INPUT", "message": "과거 출하자료를 함께 보려면 출하통계의 품종·월·기간을 직접 선택하세요. 경매 품종에서 자동 변환하지 않습니다.",
            "missing_fields": ["shipment_variety", "target_month"]}})
    else:
        entries.append({"tool": "get_historical_shipment_context", "result": get_historical_shipment_context(shipment_query)})
    if market_query is not None:
        entries.append({"tool": "get_market_evidence", "result": get_market_evidence(market_query)})
    if loss_values is not None or price_values is not None:
        entries.append({"tool": "compare_sensitivity", "result": compare_sensitivity(scenario, option_id, loss_values, price_values)})
    if entries[0]["result"]["status"] == "OK" or loss_values is not None or price_values is not None:
        entries.append({"tool": "explain_decision_conditions", "result": decision_conditions(scenario, option_id, loss_values, price_values)})
    statuses = [{"tool": entry["tool"], "status": entry["result"]["status"]} for entry in entries]
    complete = all(item["status"] == "OK" for item in statuses)
    return {"status": "OK", "analysis_type": "review_brief", "review_status": "COMPLETE" if complete else "PARTIAL",
            "components": deepcopy(entries), "component_statuses": statuses,
            "message": "선택한 자료와 입력 조건을 함께 검토했습니다." if complete else "확인된 부분 결과입니다. 누락·미관측·자료 오류 항목을 확인하세요.",
            "warnings": ["과거 출하량과 경락가격은 별도 관측입니다. 두 자료의 인과관계·현재 수급·미래가격을 추정하지 않습니다.",
                         "경락가격을 농가 제시가격으로 자동 대입하지 않았습니다. 출하 분류와 경매 품종을 자동 매핑하지 않았습니다."],
            "is_future_forecast": False, "recommended_option": None}
