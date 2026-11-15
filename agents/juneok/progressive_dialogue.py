"""Short, state-derived follow-up questions for shipment comparisons.

Presentation only. Never fills missing costs, forecasts prices, or changes the
validated calculation/state machines. The authoritative result determines what
is still missing; numbers are computed only from explicit stored inputs.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re


MENU_NOW = "감귤을 지금 팔 때와 더 보관하고 팔 때를 비교해 주세요."
MENU_FUTURE = "두 미래 출하일에 감귤을 팔 때를 비교해 주세요."


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _won(value: Decimal) -> str:
    return f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}".rstrip("0").rstrip(".")


def _known_amount(quantity, price, price_basis, *, first_future=False):
    q, p = _positive(quantity), _positive(price)
    if q is None or p is None or price_basis not in {"gross", "commission_net"}:
        return None
    if first_future:
        label = "수수료·감모·보관비 차감 전 단순 금액" if price_basis == "gross" else "감모·별도 비용 반영 전 단순 금액"
        return (f"입력한 **{_won(q)}kg × {_won(p)}원/kg**은 **{_won(q * p)}원**입니다. "
                f"이는 A안의 {label}이며, 출하 후 비교금액은 아닙니다.")
    if price_basis == "gross":
        return (f"입력하신 **{_won(q)}kg**을 **{_won(p)}원/kg**에 모두 판다면, "
                f"**수수료·기타 비용 차감 전 판매금액은 {_won(q * p)}원**입니다.")
    return (f"입력하신 **{_won(q)}kg**을 **{_won(p)}원/kg**에 모두 판다면, "
            f"**수수료 차감 후·기타 비용 차감 전 금액은 {_won(q * p)}원**입니다.")


def current_comparison_guidance(result: dict, deferred_fields=None) -> str | None:
    """Progressive prompt for current sale vs storage, without new assumptions."""
    if result.get("status") != "NEEDS_INPUT" or not isinstance(result.get("missing_fields"), list):
        return None
    all_missing = result["missing_fields"]
    deferred = set(deferred_fields or ()) & set(all_missing)
    missing = [field for field in all_missing if field not in deferred]
    data = result.get("input")
    if not isinstance(data, dict):
        return None
    subtotal = _known_amount(data.get("quantity_kg"), data.get("current_price_per_kg"), data.get("price_basis"))
    if not missing:
        return ("아직 **확인되지 않은 조건**이 남아 있습니다. 지금은 금액을 확정할 수 없습니다. "
                "관련 명세서에서 확인한 값을 알려주시거나, 별도로 사용할 가정값을 직접 정해 주세요. "
                "미확인 항목을 0이나 추정치로 바꾸지 않겠습니다.")
    if "quantity_kg" in missing and "current_price_per_kg" in missing:
        question = "비교할 **감귤 물량**과 **지금 판매할 때 kg당 제시가격**을 알려주세요. (예: 3톤, 3,830원/kg)"
    elif "quantity_kg" in missing:
        question = "비교할 **감귤 물량**은 몇 kg인가요? 톤으로 알려주셔도 됩니다."
    elif "current_price_per_kg" in missing:
        question = "**지금 팔 때 실제로 제시받은 kg당 가격**은 얼마인가요?"
    elif "price_basis" in missing:
        question = "그 가격은 **판매수수료를 빼기 전**인가요, **뺀 뒤**인가요?"
    elif "options" in missing:
        question = "**며칠 더 보관한 뒤** 판매하는 경우와 비교할까요? (예: 3일, 7일, 14일)"
    elif "cost_policy" in missing:
        question = ("선별·포장·운송비 등 **보관비 이외의 비용이 지금과 나중에 같아서 비교에서 제외해도 될까요?** "
                    "다르면 각각 알려주세요.")
    elif "included_costs" in missing:
        question = "입력한 kg당 가격에 **이미 반영된 비용**이 있나요? 없다면 '없음'이라고 알려주세요."
    elif "current_fee_percent" in missing:
        question = "**지금 팔 때 판매수수료율**은 몇 %인가요? 모르시면 미확인이라고 알려주세요."
    elif "current_costs" in missing:
        question = "지금 팔 때 **별도로 지출할 비용**이 있나요? 확인한 항목과 금액을 알려주세요."
    else:
        option_by_id = {o.get("id"): o for o in data.get("options", []) if isinstance(o, dict)}
        question = None
        for field in missing:
            parts = field.split(".")
            if len(parts) != 3 or parts[0] != "options":
                continue
            option = option_by_id.get(parts[1], {})
            days = option.get("delay_days")
            name = f"{days}일 보관 대안" if days else "보관 대안"
            if parts[2] == "delay_days":
                question = "비교하려는 **보관기간**은 며칠인가요?"
            elif parts[2] == "loss_percent":
                question = f"**{name}**의 **기간 전체 감모율**(판매 가능한 물량 감소율)은 몇 %로 예상하시나요?"
            elif parts[2] == "daily_storage_cost":
                question = f"**{name}**의 전체 물량 기준 **하루 보관비**는 얼마인가요?"
            elif parts[2] == "fee_percent":
                question = f"**{name}**에서 판매수수료율은 몇 %인가요?"
            elif parts[2] == "costs":
                question = f"**{name}**에 별도로 드는 선별·포장·운송비 등이 있나요?"
            if question:
                break
        if question is None:
            question = "아직 확인되지 않은 비용·보관 조건이 있습니다. 알고 계신 조건을 한 가지씩 알려주세요."
    notes = [subtotal] if subtotal else []
    if deferred:
        labels = {"price_basis": "판매수수료 차감 전·후 기준", "current_fee_percent": "판매수수료율"}
        names = [labels.get(field, "감모율" if field.endswith("loss_percent") else
                            "하루 보관비" if field.endswith("daily_storage_cost") else "비용 조건")
                 for field in sorted(deferred)]
        notes.append("**미확인으로 보류:** " + ", ".join(dict.fromkeys(names)) + ". 확인 전에는 손익을 확정하지 않습니다.")
    if notes:
        notes.append("**다음으로 확인할 내용**")
    note = "\n\n".join(notes)
    return (note + "\n\n" if note else "") + question + "\n\n모르는 항목은 추정하거나 0원으로 처리하지 않겠습니다."


def future_pair_guidance(result: dict, pair_inputs: dict | None = None) -> str | None:
    """Ask one decision-topic for a two-future-date comparison."""
    if result.get("status") != "NEEDS_INPUT" or not isinstance(result.get("missing_fields"), list):
        return None
    if result.get("source_status") in {"NEEDS_CLARIFICATION", "BOX_PRICE_CONFIRMATION"} or result.get("manual_price_required"):
        return None
    fields = set(result["missing_fields"])
    if not fields:
        return None
    inputs = pair_inputs if isinstance(pair_inputs, dict) else {}
    start = inputs.get("start_date")
    a = inputs.get("option_a") if isinstance(inputs.get("option_a"), dict) else {}
    b = inputs.get("option_b") if isinstance(inputs.get("option_b"), dict) else {}
    # Direct date-edit actions render without passing pair_inputs. The validated
    # pending result still carries both periods; use it before asking again.
    periods = result.get("storage_days") if isinstance(result.get("storage_days"), dict) else {}

    def period(side, option):
        days = option.get("delay_days")
        if days is None:
            days = periods.get(f"{side}안")
        return f"{days}일" if type(days) is int and 1 <= days <= 365 else "해당 기간"

    dates = {"보관 시작일", "A안 보관일", "B안 보관일"}
    if fields & dates:
        question = ("먼저 **어느 날짜와 어느 날짜**에 출하하는 경우를 비교할까요? "
                    "(예: 11월 18일과 11월 25일. 보관 시작일이 오늘과 다르면 함께 알려주세요.)")
    elif "비교 물량(kg)" in fields:
        question = "두 날짜에 비교할 **감귤 물량**은 얼마나 되나요? (kg이나 톤으로 알려주세요.)"
    elif "A안 가정 단가(원/kg)" in fields:
        question = "**먼저 출하할 때** kg당 얼마를 받을 것으로 생각하시나요?"
    elif "수수료 차감 전·후 기준" in fields:
        question = "그 판매가격은 **판매수수료를 빼기 전**인가요, **뺀 뒤**인가요?"
    elif "A안 기간 전체 감모율(%)" in fields or "B안 기간 전체 감모율(%)" in fields:
        if "A안 기간 전체 감모율(%)" in fields and "B안 기간 전체 감모율(%)" in fields:
            question = (f"먼저 {period('A', a)} 보관 후 출하할 때와 나중 {period('B', b)} 보관 후 출하할 때 "
                        "**기간 전체 감모율**을 각각 몇 %로 예상하시나요? "
                        "(모르면 미확인이라고 말씀해 주세요.)")
        elif "A안 기간 전체 감모율(%)" in fields:
            question = f"먼저 출하하기까지 {period('A', a)} 보관하는 동안 감모율은 몇 %인가요?"
        else:
            question = f"나중 출하하기까지 {period('B', b)} 보관하는 동안 감모율은 몇 %인가요?"
    elif any("하루 보관비" in field for field in fields):
        question = "**전체 물량의 하루 보관비**는 얼마인가요? 두 날짜에 동일하면 금액 하나만 알려주세요."
    elif any("별도 비용" in field for field in fields):
        question = "**보관비 외 별도 비용**은 얼마인가요? 두 날짜에 동일하면 한 번만 알려주세요."
    elif any("수수료율" in field for field in fields):
        question = "**판매수수료율**은 몇 %인가요? 두 날짜에 동일하면 한 번만 알려주세요."
    elif "B안 가정 단가(원/kg)" in fields:
        question = ("**나중 출하할 때의 kg당 가격**을 알고 계신가요? "
                    "모르면 '미정'이라고 알려주세요. 아직 가격이 확정되지 않았다면 미래 손익을 단정할 수 없습니다.")
    else:
        question = "남은 비교 조건 중 확인하신 항목을 하나만 알려주세요."
    known = _known_amount(inputs.get("quantity"), inputs.get("price_a"), inputs.get("price_basis"), first_future=True)
    preview = result.get("history_preview")
    prefix = []
    if isinstance(preview, dict) and isinstance(preview.get("clean_pairs"), int):
        prefix.append(f"같은 규격의 **과거 가격쌍 {preview['clean_pairs']}개**가 조회됐습니다. 이는 미래 판매가격 예측이 아닙니다.")
    if known:
        prefix.append(known)
    prefix.append("**다음으로 확인할 내용**")
    return "\n\n".join(prefix) + "\n\n" + question + "\n\n미확인 비용이나 감모율을 임의로 채우지 않겠습니다."


def quickstart_reply(session, question, render_dialogue, report_for):
    """Start the two exact UI examples without an otherwise unnecessary LLM call.

    Only used when no provider client has been created. Later free-text answers
    still go through the normal state-aware model/tool pipeline.
    """
    key = question.strip()
    if key not in {MENU_NOW, MENU_FUTURE}:
        return None

    from .task_context import TaskContext

    session.messages = []
    session._current_semantic_active = (key == MENU_NOW)
    session._current_prompt_field = "quantity_kg" if key == MENU_NOW else None
    session._current_unanswered = 0
    session._deferred_current_fields = set()
    session.task_context = TaskContext()
    session.price_history_query = None
    session.price_history_revision = None
    if key == MENU_NOW:
        from agents.common.calculation_service import compare_shipment_options

        changed = session.store.update({}, session.store.version, start_new=True)
        if changed.get("status") != "OK":
            return None
        session.future_pair.reset()
        tool = "compare_shipment_options"
        result = compare_shipment_options(session.store.scenario)
    else:
        tool = "compare_future_pair"
        result = session.future_pair.apply("new", session.future_pair.version, {})
    session.task_context.record(tool, {}, result, scenario=session.store.scenario)
    entry = {"tool": tool, "result": result}
    text = render_dialogue([entry], question=question, pair_inputs=session.future_pair.inputs)
    return {"status": "OK", "text": text, "report": report_for([entry]),
            "tool_results": [entry], "visible_results": [entry], "events": [],
            "state": session.store.snapshot(), "task_context": session.task_context.snapshot(),
            "metrics": {"model_requests": 0, "input_tokens": 0, "output_tokens": 0,
                        "cached_input_tokens": 0, "request_seconds": [], "request_input_characters": [],
                        "trimmed_history_turns": 0, "elapsed_seconds": 0.0},
            "termination": {"reason": "OK", "phase": "local_quickstart", "tool_calls_executed": 0}}


# This is intentionally narrow: an initial, quantity-only timing question is
# safe to capture locally; a request with quotes, dates, comparison conditions,
# or a prior active task still goes through the normal state-aware agent.
_SIMPLE_TIMING_QUESTION = re.compile(
    r"^\s*(?:제주\s*)?(?:감귤|귤|밀감)(?:은|을|이|는)?\s*"
    r"\d[\d,]*(?:\.\d+)?\s*(?:kg|킬로그램|톤|t)(?:은|을|이|는|인데|인데요)?\s*,?\s*"
    r"언제\s*(?:팔까요|팔면\s*좋을까요|파는\s*게\s*좋을까요|"
    r"출하할까요|출하하면\s*좋을까요)\s*[?!.]?\s*$",
    re.IGNORECASE,
)


def first_timing_reply(session, question: str, render_dialogue, report_for):
    """Capture a single explicit weight and ask for the quote, without LLM.

    Requires a *pristine* comparison. Never guesses a fee, sale price, storage
    duration, loss rate, or forecast. All saved numbers pass ScenarioStore.
    """
    if _SIMPLE_TIMING_QUESTION.fullmatch(question) is None:
        return None
    from .future_pair_intent import explicit_shipment_quantity
    from agents.common.calculation_service import compare_shipment_options

    quantity = explicit_shipment_quantity(question)
    if quantity is None:
        return None
    current = session.store.scenario
    from decimal import Decimal
    already_saved = current.get("quantity_kg") is not None
    if already_saved:
        # A repeated question should use confirmed inputs, not ask for them again.
        try:
            if Decimal(str(current["quantity_kg"])) != Decimal(str(quantity)):
                return None
        except (InvalidOperation, ValueError, TypeError):
            return None
    elif any(value is not None and value != [] for value in current.values()):
        # Do not mix a new quantity into an unrelated, partially filled task.
        return None
    pair = session.future_pair.inputs
    if any(value is not None for name, value in pair.items() if name not in {"option_a", "option_b"}):
        return None
    if any(value is not None for side in ("option_a", "option_b") for value in pair[side].values()):
        return None

    if not already_saved:
        from uuid import uuid4
        previous_turn = session.store.origin_turn_id
        session.store.origin_turn_id = uuid4().hex
        try:
            update = session.store.update({"quantity_kg": quantity}, session.store.version)
        finally:
            session.store.origin_turn_id = previous_turn
        if update.get("status") != "OK":
            return None
        session.task_context.record("update_scenario", {"patch": {"quantity_kg": quantity}},
                                    update, scenario=session.store.scenario)
    result = compare_shipment_options(session.store.scenario)
    session.task_context.record("compare_shipment_options", {}, result,
                                scenario=session.store.scenario)
    entry = {"tool": "compare_shipment_options", "result": result}
    reply = render_dialogue([entry], question=question)
    return {"status": "OK", "text": reply, "report": report_for([entry]),
            "tool_results": [entry], "visible_results": [entry], "events": [],
            "state": session.store.snapshot(), "task_context": session.task_context.snapshot(),
            "metrics": {"model_requests": 0, "input_tokens": 0, "output_tokens": 0,
                        "cached_input_tokens": 0, "request_seconds": [], "request_input_characters": [],
                        "trimmed_history_turns": 0, "elapsed_seconds": 0.0},
            "termination": {"reason": "OK", "phase": "local_first_timing", "tool_calls_executed": 0}}


# Clarifying an unknown *current-comparison* input is a deterministic local
# follow-up. The model must not reinterpret "I do not know the fee basis" as
# permission to look up unrelated shipment data or assume zero commission.
_UNKNOWN = re.compile(r"모르|몰라|미확인|확인\s*(?:못|할\s*수\s*없)|불명확")
_CONFUSED = re.compile(r"^\s*(?:네[?？!]?|엥[?？!]?|뭐[?？!]?|무슨\s*말(?:이에요|이죠|인가요)?[?？!]?|이해가\s*안\s*돼요[?？!]?)\s*$")
_TODAY = re.compile(r"^\s*(?:오늘|지금|당장)(?:이요|요|이죠|부터|요[.!?])?[.!?]?\s*$")
_DAYS = re.compile(r"^\s*(?:그럼\s*)?([1-9]\d{0,2})\s*일(?:\s*(?:보관|뒤|후|후에|동안|이요|요|로|로요))?[.!?]?\s*$")


def current_input_followup(session, question, render_dialogue, report_for):
    """Handle explicit uncertainty, confusion and short timing answers safely.

    Scope: now-versus-later comparisons with an already known quoted price.
    Does not choose a fee basis, loss, cost or forecast; canonical values stay
    None until the user supplies them. Other requests keep the model/tool route.
    """
    from agents.common.calculation_service import compare_shipment_options

    current = session.store.scenario
    if session.future_pair.pending is not None or current.get("current_price_per_kg") is None:
        return None
    result = compare_shipment_options(current)
    if result.get("status") != "NEEDS_INPUT":
        session._deferred_current_fields = set()
        return None
    missing = set(result["missing_fields"])
    deferred = set(getattr(session, "_deferred_current_fields", set())) & missing
    session._deferred_current_fields = deferred
    value = question.strip()
    unknown = bool(_UNKNOWN.search(value))
    confused = bool(_CONFUSED.fullmatch(value))
    today_only = bool(_TODAY.fullmatch(value))
    days_match = _DAYS.fullmatch(value)
    explicitly_about_basis = bool(re.search(r"수수료|차감|공제|뺀|빼기|실수취|가격\s*기준", value))
    explicitly_about_costs = bool(re.search(r"보관비|저장료|감모|감량|줄어드는|수수료율|기타\s*비용", value))

    def local(text):
        entry = {"tool": "compare_shipment_options", "result": compare_shipment_options(session.store.scenario)}
        return {"status": "OK", "text": text, "report": report_for([entry]),
                "tool_results": [entry], "visible_results": [entry], "events": [],
                "state": session.store.snapshot(), "task_context": session.task_context.snapshot(),
                "metrics": {"model_requests": 0, "input_tokens": 0, "output_tokens": 0,
                            "cached_input_tokens": 0, "request_seconds": [],
                            "request_input_characters": [], "trimmed_history_turns": 0,
                            "elapsed_seconds": 0.0},
                "termination": {"reason": "OK", "phase": "local_input_clarification",
                                "tool_calls_executed": 0}}

    if unknown and (explicitly_about_basis or (not deferred and "price_basis" in missing and not explicitly_about_costs)):
        if "price_basis" not in missing:
            return None
        deferred.add("price_basis")
        session._deferred_current_fields = deferred
        return local("판매수수료를 뺀 가격인지는 **미확인으로 유지**하겠습니다. "
                     "입력하신 물량과 kg당 가격은 그대로 보존합니다. "
                     "수수료 기준을 확인하기 전에는 실수취 금액을 확정하지 않겠습니다.\n\n"
                     "**다음으로 확인할 내용**\n\n"
                     "지금 출하는 비교 기준으로 두고, **며칠 더 보관한 경우**와 비교할까요? "
                     "(예: 3일, 7일, 14일)")

    if unknown and explicitly_about_costs:
        key = None
        if re.search(r"감모|감량|줄어드는", value):
            key = next((k for k in missing if k.endswith(".loss_percent")), None)
        elif re.search(r"보관비|저장료", value):
            key = next((k for k in missing if k.endswith(".daily_storage_cost")), None)
        elif "수수료율" in value:
            key = "current_fee_percent" if "current_fee_percent" in missing else next(
                (k for k in missing if k.endswith(".fee_percent")), None)
        if key:
            deferred.add(key)
            session._deferred_current_fields = deferred
            guidance = current_comparison_guidance(result, deferred_fields=deferred)
            return local("말씀하신 값은 **미확인**으로 두겠습니다.\n\n" + guidance)
        return None

    if "price_basis" not in missing and not deferred:
        return None

    if confused and "price_basis" in missing and "price_basis" not in deferred:
        return local("판매수수료를 이미 뗀 가격인지 확인하려는 질문입니다. "
                     "예를 들어 **수수료 차감 전 2,275원**이라면 수수료가 추가로 빠지고, "
                     "**차감 후 2,275원**이라면 같은 수수료를 다시 빼면 안 됩니다. "
                     "모르시면 **'미확인'**이라고 답해도 됩니다.")

    if deferred and (confused or today_only):
        first = ("가격의 수수료 기준은 **미확인으로 유지**하고 있습니다. "
                 "확인할 수 있을 때 알려주시면 됩니다. " if confused else
                 "**오늘 출하**는 이미 비교 기준입니다. ")
        if current["options"]:
            guidance = current_comparison_guidance(result, deferred_fields=deferred)
            return local(first + "\n\n" + guidance)
        return local(first + "\n\n지금과 비교할 **추가 보관기간**을 알려주세요. "
                     "예: **7일 보관**. 보관비·감모율은 확인하기 전까지 계산에 넣지 않습니다.")

    if deferred and days_match and "options" in missing:
        days = int(days_match.group(1))
        if not 1 <= days <= 365:
            return None
        # Preserve an existing option ID; never reuse a prior period's loss.
        if len(current["options"]) > 1:
            return None
        option = dict(current["options"][0]) if current["options"] else {"id": "storage_comparison"}
        if option.get("delay_days") != days:
            option["loss_percent"] = None
            option["loss_confirmed_for_period"] = False
        option["delay_days"] = days
        changed = session.store.update({"options": [option]}, session.store.version)
        if changed.get("status") != "OK":
            return None
        session.task_context.record("update_scenario", {"patch": {"options": [option]}},
                                    changed, scenario=session.store.scenario)
        updated = compare_shipment_options(session.store.scenario)
        guidance = current_comparison_guidance(updated, deferred_fields=deferred)
        return local(f"**{days}일 보관 후 출하**를 비교 대안으로 저장했습니다.\n\n" + (guidance or ""))

    # Bare 'I don't know' after another deferred item must not reset a known
    # quantity or turn into a shipment-statistics search.
    if deferred and unknown and not explicitly_about_basis:
        guidance = current_comparison_guidance(result, deferred_fields=deferred)
        return local("확인되지 않은 값은 그대로 두겠습니다.\n\n" + (guidance or ""))
    return None
