"""One verified result set for the conversation, export, and acceptance checks."""
from __future__ import annotations

from copy import deepcopy
from .reporting import render_report, render_result, FIELD_NAMES, _field


def select_results(results: list[dict], version: int) -> list[dict]:
    expanded = []
    for entry in results:
        if entry["tool"] == "build_review_brief" and entry["result"].get("analysis_type") == "review_brief":
            if entry["result"].get("scenario_version") != version:
                continue
            summary = {k: v for k, v in entry["result"].items() if k != "components"}
            expanded.append({"tool": "build_review_brief", "result": summary})
            for component in entry["result"]["components"]:
                item = deepcopy(component)
                item["result"]["scenario_version"] = version
                expanded.append(item)
        else:
            expanded.append(entry)
    results = expanded
    visible = [x for x in results if x["tool"] != "update_scenario" and
               (x["tool"] not in {"compare_shipment_options", "compare_sensitivity", "explain_decision_conditions", "build_review_brief", "solve_offer_limits", "review_case_changes"} or x["result"].get("scenario_version") == version)]
    sensitivities = [x for x in visible if x["tool"] == "compare_sensitivity"]
    if sensitivities:
        visible = [x for x in visible if x["tool"] != "compare_sensitivity"] + [sensitivities[-1]]
    comparisons = [x for x in visible if x["tool"] == "compare_shipment_options"]
    if comparisons:
        visible = [x for x in visible if x["tool"] != "compare_shipment_options"] + [comparisons[-1]]
    return deepcopy(visible or [x for x in results if x["result"].get("status") != "OK"])


def render_dialogue(results: list[dict]) -> str:
    """Concise questions; canonical Python numbers and evidence remain unchanged."""
    sections = []
    for entry in results:
        result = entry["result"]
        if result.get("analysis_type") == "price_history":
            sections.append(render_result(result))
        elif result.get("status") == "NEEDS_INPUT" and result.get("missing_fields"):
            fields = result["missing_fields"]
            first, rest = fields[:3], fields[3:]
            prompts = {
                "quantity_kg": "비교할 감귤은 몇 kg인가요? 톤으로 알려주셔도 됩니다.",
                "current_price_per_kg": "지금 실제로 제시받은 가격은 kg당 얼마인가요?",
                "price_basis": "그 가격은 판매수수료를 뺀 금액인가요, 빼기 전인가요?",
                "included_costs": "그 단가에서 이미 차감한 비용은 무엇인가요? 예: 수수료만 / 수수료·포장비 / 없음.",
                "cost_policy": "보관비 외 선별·포장·운송비는 두 출하 시점에서 같나요? 다르면 각각 알려주세요.",
                "options": "며칠 보관하는 대안과 비교할까요?",
                "loss_values": "민감도 표에 사용할 기간 전체 감모율 가정은 각각 몇 %인가요? 알고 있는 값만 직접 정해 주세요.",
            }
            def prompt(field):
                if field in prompts:
                    return prompts[field]
                if field.endswith(".loss_percent"):
                    return f"{_field(field)}을 알려주세요. 하루 비율이 아니라 해당 보관기간 전체 비율입니다."
                if field.endswith(".daily_storage_cost"):
                    return f"{_field(field)}은 얼마인가요? 전체 물량의 하루 비용이며, 없으면 0원이라고 확인해 주세요."
                return f"{_field(field)}을 알려주세요."
            text = "계산하려면 아래 조건을 먼저 확인해야 합니다.\n\n" + "\n".join(f"- {prompt(f)}" for f in first)
            if rest:
                text += "\n\n이후 확인할 조건: " + ", ".join(_field(f) for f in rest) + ". 알고 계시면 함께 알려주세요."
            text += "\n\n모르는 값은 ‘미확인’으로 두겠습니다. 가정으로 비교하려면 사용할 값을 직접 정해 주세요."
            sections.append(text)
        else:
            sections.append(render_result(result))
    return "\n\n".join(sections)


def connection_label(key_present: bool, last_reply: dict | None) -> str:
    if not key_present:
        return "API 키 미설정 · 직접 도구 사용 가능"
    if not last_reply:
        return "API 키 설정됨 · 실제 호출 전"
    metrics = last_reply.get("metrics", {})
    if last_reply.get("status") in {"API_ERROR", "CONNECTION_ERROR", "API_TIMEOUT", "AUTH_ERROR", "RATE_LIMITED"}:
        return "마지막 모델 호출 실패 · 설정·연결 확인 필요"
    if last_reply.get("status") == "INTERNAL_ERROR":
        return "실행 처리 오류 · 직접 비교 가능, 운영자 점검 필요"
    if last_reply.get("status") in {"TIME_LIMIT", "TOOL_LIMIT", "REPEATED_TOOL_CALL", "CONTEXT_LIMIT", "LIMIT_REACHED"}:
        return "실행 한도 도달 · 조건 유지, 직접 비교 가능"
    if metrics.get("model_requests", 0) and last_reply.get("status") == "OK":
        return "실제 모델 호출 성공 · 마지막 실행 기준"
    return "모델 실행 미완료 · 실행 상태 확인 필요"


def report_for(results: list[dict]) -> str:
    return render_report(results) if results else "확인된 조회·계산 결과가 없습니다."
