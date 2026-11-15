"""Single semantic intake + deterministic state machine for now-vs-storage.

The model's ONLY task here is to describe what the speaker meant. It cannot
choose an application tool, set default assumptions or calculate proceeds.
The authoritative ScenarioStore and compare_shipment_options remain intact.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4
from decimal import Decimal, InvalidOperation
import json
import time

# Strictly defined field names, not a list of sentences or keyword templates.
_FIELDS = ("quantity_kg", "current_price_per_kg", "price_basis", "delay_days",
           "loss_percent", "daily_storage_cost", "current_fee_percent", "fee_percent",
           "cost_policy", "included_costs", "future_price_per_kg")
_COST_KINDS = ("commission", "sorting", "packaging", "transport", "storage", "other")

def _nullable(t, **kwargs):
    return {"type": [t, "null"], **kwargs}

_SCHEMA = {"type": "object", "additionalProperties": False,
           "properties": {
               "task": {"type": "string", "enum": ["current_comparison", "other"]},
               "intent": {"type": "string", "enum": ["provide", "unknown", "clarify", "other"]},
               "unknown_fields": {"type": "array", "items": {"type": "string", "enum": list(_FIELDS)}},
               "evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                   "properties": {"field": {"type": "string", "enum": list(_FIELDS)},
                                  "quote": {"type": "string"}}, "required": ["field", "quote"]}},
               "quantity_kg": _nullable("number"),
               "current_price_per_kg": _nullable("number"),
               "price_basis": _nullable("string", enum=["gross", "commission_net", None]),
               "delay_days": _nullable("integer"),
               "loss_percent": _nullable("number"),
               "daily_storage_cost": _nullable("number"),
               "current_fee_percent": _nullable("number"),
               "fee_percent": _nullable("number"),
               "cost_policy": _nullable("string", enum=["equal_excluded", "itemized", None]),
               "included_costs": {"type": ["array", "null"],
                                  "items": {"type": "string", "enum": list(_COST_KINDS)}},
               "future_price_per_kg": _nullable("number"),
           }}
_SCHEMA["required"] = list(_SCHEMA["properties"])
_PARSE_TOOL = {"type": "function", "name": "interpret_current_shipment_turn", "strict": True,
               "description": "사용자 발화에서 명시된 사실과 의도만 구조화. 계산하거나 가정하지 말 것.",
               "parameters": _SCHEMA}

_INSTRUCTIONS = """당신은 제주 감귤 출하 도우미의 입력 해석기입니다. 결론이나 대답을 하지 말고 함수만 호출하세요.
현재 목적: '지금 파는 것'과 '나중에 보관 후 파는 것'을 비교할 조건을 수집합니다.
의미를 해석하되 문장·단어 패턴에 의존하지 마세요. 같은 뜻의 다양한 표현을 같은 필드로 정규화하세요.
- 최초 '며칠 뒤 출하할까', '언제 팔까', '보관이 나을까', 두 선택 비교는 current_comparison입니다.
- 이전 비교가 진행 중인 동안 짧은 숫자·답변·정정은 그 흐름을 이어갑니다. 다른 주제(과거 거래자료만 조회, 정산, 두 개의 서로 다른 미래 출하일 A/B 등)는 other입니다.
- quantity_kg은 전체 물량(톤→1000kg), current_price_per_kg은 지금 판매 제시단가(원/kg)입니다. 상자 단가나 미래 단가는 절대로 현재 단가로 바꾸지 마세요.
- delay_days는 지금 즉시 출하와 비교할 '추가 보관기간'입니다. 정확한 일주일=7일. '다음 주'처럼 정확한 일수가 모호하면 null로 둡니다. '오늘'은 0일 추가 보관 대안이 아니라 이미 존재하는 지금 판매 기준입니다.
- price_basis gross는 수수료 차감 전, commission_net은 수수료 차감 후가 사용자가 명확히 확인한 경우에만 지정합니다. 모호하면 null.
- loss_percent는 해당 보관기간 전체 감소율, daily_storage_cost는 전체 물량의 하루 보관비입니다. 비용 미확인은 0이 아닙니다.
- cost_policy equal_excluded는 사용자가 선별·포장·운송 등 보관비 외 비용이 지금과 나중에 같아 비교에서 제외해도 된다고 명시한 경우에만 사용합니다.
- included_costs는 사용자가 그 제시가격에 이미 반영된 비용 항목을 명시한 경우에만 설정합니다. '없음'을 확답했을 때만 빈 배열을 사용합니다.
- unknown: 사용자가 무지·미확인을 진술한 경우. 해당 필드가 분명하면 unknown_fields로 지정합니다. 문맥상 직전 질문의 답인지 명확하면 unknown_fields는 비워도 됩니다.
- clarify: 직전 질문을 이해하지 못하거나 뜻·확인 방법을 묻는 경우입니다.
- provide: 하나 이상의 입력 값 제공 또는 명확한 대화 진행 요청.
- other: 무관한 발화, 질문에 답하지 않은 발화. 명확한 새로운 주제일 경우 task도 other.
- 모르는 값, 수수료, 가격, 예상 감모, 관측 자료는 추정·보간·생성하지 않습니다. 불확실하면 null.
- 값이 변경됐을 때도 이번 메시지에서 명시한 필드만 전달합니다. 과거 값을 반복하거나 null로 초기화하지 않습니다.
- null이 아닌 값을 추출했다면 evidence에 해당 필드 이름과 이번 사용자 발화에서 글자 그대로 복사한 짧은 인용 구절을 반드시 넣으세요. 이전 맥락에서 가져온 말은 이번 증거로 쓸 수 없습니다.
"""

_EXPLANATIONS = {
    "quantity_kg": "비교할 감귤 전체 무게입니다. kg이나 톤으로 말씀해 주세요.",
    "current_price_per_kg": "현재 판매처에서 제시받은 감귤 1kg의 가격입니다. 아직 가격을 모르셔도 됩니다.",
    "price_basis": "수수료 차감 전 가격이라면 판매수수료를 이후에 빼야 하고, 차감 후라면 다시 빼면 안 됩니다. 거래명세서나 판매처에 확인할 수 있습니다.",
    "options": "오늘 판매와 비교할 '추가 보관기간'입니다. 예를 들어 7일 뒤에 출하할지 비교하려면 7일이라고 알려주세요.",
    "cost_policy": "선별·포장·운송비처럼 보관료 이외 비용이 지금과 나중에 똑같다면 비교에서 제외할 수 있습니다. 같다고 확신하지 못하면 미확인으로 둡니다.",
    "included_costs": "판매처가 제시한 kg당 단가에 이미 빠져 있는 비용을 확인합니다. 같은 비용을 중복 차감하지 않기 위해서입니다.",
    "current_fee_percent": "현재 출하 시 판매수수료가 판매금액의 몇 %인지 확인하는 항목입니다. 모르면 미확인으로 남깁니다.",
    "delay_days": "보관 후 출하까지 오늘부터 며칠이 추가로 필요한지입니다.",
    "loss_percent": "보관하는 기간 전체에 팔 수 있는 감귤이 줄어드는 비율입니다. 하루 감모율이 아닙니다.",
    "daily_storage_cost": "감귤 전체 물량을 하루 보관하는 비용입니다. 실제로 무료라고 확인된 경우에만 0원으로 입력합니다.",
    "fee_percent": "보관 후 판매 시 수수료율입니다. 현재 수수료와 같다고 확인하지 않았다면 따로 확인합니다.",
}

def _make_client(session):
    if session.client is None:
        from .agent import load_model_sdk
        from .config import Settings
        settings = session.settings or Settings.load()
        session.client = load_model_sdk()(api_key=settings.api_key, timeout=30, max_retries=0)
    return session.client


def _interpret(session, question, active, waiting):
    from .config import Settings
    settings = session.settings or Settings.load()
    # Snapshot is authoritative; the provider cannot rewrite it.
    context = {"active": bool(active), "awaiting": waiting,
               "known": {key: value for key, value in session.store.scenario.items()
                         if key != "options" and value is not None},
               "options": session.store.scenario["options"],
               "deferred": sorted(getattr(session, "_deferred_current_fields", set())),
               "user": question}
    started = time.monotonic()
    client = _make_client(session)
    if hasattr(client, "with_options"):
        client = client.with_options(timeout=30, max_retries=0)
    response = client.responses.create(
        model=settings.model, instructions=_INSTRUCTIONS,
        input=[{"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}],
        tools=[_PARSE_TOOL], tool_choice={"type": "function", "name": _PARSE_TOOL["name"]},
        parallel_tool_calls=False, store=False, max_output_tokens=900,
    )
    item = next((o for o in response.output if getattr(o, "type", None) == "function_call"
                 and getattr(o, "name", None) == _PARSE_TOOL["name"]), None)
    if item is None:
        raise ValueError("대화 입력 구조화 결과가 없습니다.")
    result = json.loads(item.arguments)
    if not isinstance(result, dict) or set(result) != set(_SCHEMA["required"]):
        raise ValueError("대화 입력 계약이 맞지 않습니다.")
    if result["task"] not in ("current_comparison", "other") or result["intent"] not in ("provide", "unknown", "clarify", "other"):
        raise ValueError("대화 의도 값이 유효하지 않습니다.")
    # Every asserted non-null value must have literal user-message evidence.
    citations = result.get("evidence")
    if not isinstance(citations, list):
        raise ValueError("추출 근거가 없습니다.")
    for key in _FIELDS:
        if result.get(key) is None:
            continue
        if not any(isinstance(item, dict) and item.get("field") == key and
                   isinstance(item.get("quote"), str) and
                   0 < len(item["quote"]) <= 120 and item["quote"] in question
                   for item in citations):
            raise ValueError(f"{key}: 사용자 메시지의 근거가 없습니다.")
    usage = getattr(response, "usage", None)
    return result, time.monotonic()-started, usage


def _is_finite_number(value, *, positive=False, max_limit=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        amount = Decimal(str(value))
        return amount.is_finite() and (amount > 0 if positive else amount >= 0) and (max_limit is None or amount < max_limit)
    except (InvalidOperation, ValueError, TypeError):
        return False


def _fields_present(values):
    """Validate all extracted fields independently before any state mutation."""
    patch = {}
    for field in ("quantity_kg", "current_price_per_kg"):
        value = values[field]
        if value is not None:
            if not _is_finite_number(value, positive=True):
                raise ValueError(f"{field}: 양수인 실제 입력값이 필요합니다.")
            patch[field] = value
    for field in ("current_fee_percent",):
        value = values[field]
        if value is not None:
            if not _is_finite_number(value, max_limit=100):
                raise ValueError(f"{field}: 0 이상 100 미만이어야 합니다.")
            patch[field] = value
    for field, options in (("price_basis", ("gross", "commission_net")),
                           ("cost_policy", ("equal_excluded", "itemized"))):
        if values[field] is not None:
            if values[field] not in options:
                raise ValueError(f"{field}: 확인된 값이 아닙니다.")
            patch[field] = values[field]
    if values["included_costs"] is not None:
        value = values["included_costs"]
        if not isinstance(value, list) or any(v not in _COST_KINDS for v in value) or len(value) != len(set(value)):
            raise ValueError("단가에 포함된 비용 항목을 확인해 주세요.")
        patch["included_costs"] = value
    option_patch = {}
    days = values["delay_days"]
    if days is not None:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValueError("보관기간은 1~365일이어야 합니다. 오늘 판매는 별도 기준입니다.")
        option_patch["delay_days"] = days
    for key in ("loss_percent", "fee_percent"):
        value = values[key]
        if value is not None:
            if not _is_finite_number(value, max_limit=100):
                raise ValueError(f"{key}: 0 이상 100 미만이어야 합니다.")
            option_patch[key] = value
    for key in ("daily_storage_cost", "future_price_per_kg"):
        value = values[key]
        if value is not None:
            if not _is_finite_number(value):
                raise ValueError(f"{key}: 0 이상의 금액이어야 합니다.")
            option_patch[key] = value
    return patch, option_patch


def _apply(session, top_patch, option_patch):
    current = session.store.scenario
    updates = dict(top_patch)
    if option_patch:
        existing = current.get("options", [])
        if len(existing) > 1:
            raise ValueError("보관 대안이 여러 개입니다. 직접 입력에서 어떤 대안을 변경할지 선택해 주세요.")
        old = existing[0] if existing else {"id": "storage_comparison"}
        option = deepcopy(old)
        # Changing delay invalidates the loss for the old entire period.
        if "delay_days" in option_patch and option_patch["delay_days"] != option.get("delay_days"):
            if "loss_percent" not in option_patch:
                option["loss_percent"] = None
            else:
                option["loss_percent"] = option_patch["loss_percent"]
                option["loss_confirmed_for_period"] = True
        option.update(option_patch)
        updates["options"] = [option]
    if not updates:
        return False
    if all(current.get(k) == v for k, v in updates.items()):
        return False
    previous_origin = session.store.origin_turn_id
    session.store.origin_turn_id = uuid4().hex
    try:
        saved = session.store.update(updates, session.store.version)
    finally:
        session.store.origin_turn_id = previous_origin
    if saved.get("status") != "OK":
        raise ValueError(saved.get("message", "입력값을 저장할 수 없습니다."))
    session.task_context.record("update_scenario", {"patch": updates}, saved, scenario=session.store.scenario)
    return True


def _missing_as_input(result, deferred):
    original = result.get("missing_fields", [])
    return [key for key in original if key not in deferred]


def _prompt_field(result, deferred):
    """One canonical priority order shared by question generation and state."""
    missing = _missing_as_input(result, deferred)
    if not missing:
        return None
    for key in ("quantity_kg", "current_price_per_kg", "price_basis", "options",
                "cost_policy", "included_costs", "current_fee_percent", "current_costs"):
        if key in missing:
            return key
    return missing[0]


def _response(session, result, message, elapsed, usage, *, success=True):
    from .presentation import report_for
    entry = {"tool": "compare_shipment_options", "result": result}
    stats = {"model_requests": 1, "input_tokens": getattr(usage, "input_tokens", 0) or 0,
             "output_tokens": getattr(usage, "output_tokens", 0) or 0,
             "cached_input_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0,
             "request_seconds": [round(elapsed, 3)], "request_input_characters": [],
             "trimmed_history_turns": 0, "elapsed_seconds": round(elapsed, 3)}
    return {"status": "OK" if success else "INVALID_INPUT", "text": message,
            "report": report_for([entry]), "visible_results": [entry], "tool_results": [entry],
            "events": [], "state": session.store.snapshot(),
            "task_context": session.task_context.snapshot(), "metrics": stats,
            "termination": {"reason": "OK" if success else "INVALID_INPUT", "phase": "semantic_intake",
                            "tool_calls_executed": 0}}


def route_turn(session, question: str):
    """Return a complete reply for a now-vs-storage conversation; else None.

    One model call extracts only user-supplied facts. Python owns all field
    changes, deferred values, next questions and comparison arithmetic.
    """
    if session.experimental_forecast or session.future_pair.pending is not None:
        return None
    from .future_pair_intent import starts_new_comparison
    from agents.common.calculation_service import compare_shipment_options
    from .progressive_dialogue import current_comparison_guidance

    # Switching or new explicitly requested comparisons remain on the existing
    # UI and agent pathways, which already reset their own state safely.
    if starts_new_comparison(question):
        return None
    s = session.store.scenario
    deferred = set(getattr(session, "_deferred_current_fields", set()))
    before = compare_shipment_options(s)
    waiting = getattr(session, "_current_prompt_field", None)
    if waiting not in before.get("missing_fields", []):
        waiting = _prompt_field(before, deferred)
    active = bool(getattr(session, "_current_semantic_active", False)) or (
        session.future_pair.pending is None and any(s.get(field) is not None for field in
        ("quantity_kg", "current_price_per_kg")) and before.get("status") == "NEEDS_INPUT")
    try:
        intent, elapsed, usage = _interpret(session, question, active, waiting)
    except Exception:
        # During an active comparison, a failed semantic parse must never
        # resurrect the old unconstrained tool-selection path. Keep state.
        if active:
            advice = current_comparison_guidance(before, deferred_fields=deferred)
            return _response(session, before,
                             "이번 답변의 뜻을 확실히 판단하지 못해 조건을 변경하지 않았습니다.\n\n"
                             + (advice or "확인한 입력값을 한 번 더 알려주세요."), 0, None, success=False)
        # Outside this workflow, the existing specialist tools remain in charge.
        return None

    if intent["task"] != "current_comparison":
        return None
    if intent["intent"] == "other" and not active:
        return None
    try:
        # Unknown is an explicit refusal to provide a value. Even if the
        # model simultaneously returns a conflicting numeric/basis value,
        # never silently convert uncertainty into a confirmed fact.
        if intent["intent"] in ("clarify", "other"):
            top, option = {}, {}
        else:
            asserted = dict(intent)
            if intent["intent"] == "unknown":
                for field in intent.get("unknown_fields", []):
                    if field in _FIELDS:
                        asserted[field] = None
            top, option = _fields_present(asserted)
        changed = _apply(session, top, option) if top or option else False
    except ValueError as exc:
        result = compare_shipment_options(session.store.scenario)
        return _response(session, result, f"입력 조건을 확인하지 못했습니다: {exc}\n\n이 값은 저장하지 않았습니다. 확인한 조건을 다시 알려주세요.", elapsed, usage, success=False)
    session._current_semantic_active = True
    result = compare_shipment_options(session.store.scenario)
    now_missing = set(result.get("missing_fields", []))
    deferred &= now_missing
    for key in top:
        deferred.discard(key)
    for key in option:
        deferred.discard(f"options.storage_comparison.{key}")
    # Which field was unknown is a semantic choice from the model, not inferred
    # by matching the original Korean sentence. A bare "don't know" is bound
    # only to the question the deterministic planner asked last.
    if intent["intent"] == "unknown":
        marked = intent.get("unknown_fields") or []
        if not isinstance(marked, list):
            marked = []
        for key in marked:
            if key == "delay_days":
                key = "options" if "options" in now_missing else next(
                    (f for f in now_missing if f.endswith(".delay_days")), key)
            elif key in ("loss_percent", "daily_storage_cost", "fee_percent", "future_price_per_kg"):
                key = next((f for f in now_missing if f.endswith("." + key)), key)
            if key in now_missing:
                deferred.add(key)
        if not marked and waiting in now_missing:
            deferred.add(waiting)
    session._deferred_current_fields = deferred
    next_field = _prompt_field(result, deferred)
    session._current_prompt_field = next_field
    from .presentation import render_dialogue
    guidance = current_comparison_guidance(result, deferred_fields=deferred)
    if result["status"] == "OK":
        # Never invent future quotes. Comparison is made by the validated engine.
        content = render_dialogue([{"tool": "compare_shipment_options", "result": result}], question=question)
    else:
        content = guidance or "비교 조건을 확인할 수 없습니다."
    if intent["intent"] == "clarify":
        # Explain the last Python-selected field. Repeated confusion should
        # not yield identical prompts forever or silently mark unknown.
        subject = waiting
        if isinstance(subject, str) and subject.startswith("options."):
            subject = subject.rsplit(".", 1)[-1]
        text = _EXPLANATIONS.get(subject, "현재 비교에 필요한 조건을 확인 중입니다. 확인되지 않은 값은 비워 둡니다.")
        failures = min(3, getattr(session, "_current_unanswered", 0) + 1)
        session._current_unanswered = failures
        if failures >= 2:
            content = (text + "\n\n여전히 확인하기 어려우면 **'미확인'**이라고 답해 다음 조건으로 넘어갈 수 있습니다. "
                       "모른다고 말씀하시기 전에는 해당 조건을 임의로 변경하지 않습니다.")
        else:
            content = text + "\n\n" + content
    elif intent["intent"] == "unknown":
        session._current_unanswered = 0
        content = "말씀하신 조건은 **미확인**으로 유지하겠습니다. 금액을 임의로 가정하지 않습니다.\n\n" + content
    elif intent["intent"] == "other" and not changed:
        failures = min(3, getattr(session, "_current_unanswered", 0) + 1)
        session._current_unanswered = failures
        intro = ("오늘 판매는 이미 비교 기준입니다. 나중에 출하할 대안의 조건을 확인 중입니다."
                 if waiting in ("options", "delay_days") else
                 "현재 답변만으로는 확인 중인 조건이 바뀌지 않았습니다.")
        if failures >= 2:
            content = intro + "\n\n계속 답하기 어려우면 **'미확인'**으로 넘기거나 새 비교를 시작해 주세요."
        else:
            content = intro + "\n\n" + content
    else:
        session._current_unanswered = 0
    if changed:
        session.task_context.record("compare_shipment_options", {}, result, scenario=session.store.scenario)
    return _response(session, result, content, elapsed, usage)
