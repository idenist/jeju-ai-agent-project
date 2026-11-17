"""A bounded Responses loop with tool-grounded facts and natural dialogue."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
import time
from copy import deepcopy
from uuid import uuid4
from core.config import Settings
from agents.box_price import mentions_box_price, pending_box_arguments
from agents.future_pair_intent import (
    changes_later_date_only, continues_comparison, explicit_shipment_quantity, forbids_historical_pair_price,
    is_future_pair_request, requests_manual_price,
    starts_new_comparison, states_price_basis, requests_historical_pair_price,
)
from core.latency import log_latency
from agents.state import ScenarioStore, empty_scenario
from agents.tools import TOOLS, TIMING_TOOL, execute_tool, with_box_price, model_receipt_for_tool
from services.task_context import TaskContext, data_revision
from agents.harness import ExecutionPolicy, RunStopped, RepeatGuard, bound_history, encoded_size, user_starts, classify_failure, FAILURE_GUIDANCE


from agents.dialogue import has_unsupported_amount, remember_visible_reply
from agents.prompts import SYSTEM_PROMPT
from agents.service_scope import scope_reply


def _has_explicit_value(value):
    if isinstance(value, dict):
        return any(_has_explicit_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_explicit_value(item) for item in value)
    return value is not None


def load_model_sdk():
    from openai import OpenAI

    return OpenAI


@dataclass
class _TurnRouting:
    """Ephemeral routing facts for one user turn; never persisted as user data."""
    new_comparison: bool
    basis_stated: bool
    keep_pair: bool
    has_box_price: bool
    quantity: float | None
    allow_price_history: bool
    excludes_price_history: bool
    existing_reference_request: bool
    future_pair_route: bool
    comparison_started: bool = False
    pending_value_reply: bool = False


class AgentSession:
    def __init__(self, settings: Settings | None = None, client=None, policy=None):
        self.settings = settings
        self.client = client
        self.store = ScenarioStore()
        self.messages: list = []
        self.task_context = TaskContext()
        self.policy = policy or ExecutionPolicy()
        # Default OFF. Controlled A/B tool experiment only, never automatic rollout.
        self.focused_tools = os.environ.get("JEJU_FOCUSED_TOOLS", "").strip() == "1"
        self.shipment = None
        self.price_history_query = None
        self.price_history_revision = None
        from agents.future_pair_state import FuturePairState
        self.future_pair = FuturePairState()

    def _execute_requested_tool(self, name: str, arguments, question: str, routing: _TurnRouting,
                                *, on_progress=None):
        """The sole session-aware dispatch point for stateful tool calls.

        Preserve confirmed state, tool revisions and the existing execution
        order. Deliberately do not decide the next question or format results.
        """
        new_comparison = routing.new_comparison
        comparison_started = routing.comparison_started
        basis_stated = routing.basis_stated
        keep_pair = routing.keep_pair
        has_box_price = routing.has_box_price
        quantity = routing.quantity
        allow_price_history = routing.allow_price_history
        future_pair_route = routing.future_pair_route
        recorded_arguments = arguments
        if name == "get_price_history":
            from services.price_history_followup import (
                MissingPreviousPriceQuery, resolve_price_history_request,
            )
            try:
                recorded_arguments, clarification = resolve_price_history_request(arguments, self.price_history_query)
            except MissingPreviousPriceQuery:
                result = {"analysis_type": "price_history", "status": "NEEDS_INPUT",
                          "source_status": "MISSING_PREVIOUS_QUERY", "reason": "MISSING_PREVIOUS_QUERY",
                          "missing_fields": ["previous_price_history"],
                          "message": "이전에 사용한 과거 가격 조회 조건이 없습니다. 조회할 규격을 다시 알려주세요.",
                          "is_future_forecast": False}
            except ValueError as exc:
                result = {"analysis_type": "price_history", "status": "INVALID_INPUT",
                          "source_status": "INVALID_INPUT", "reason": "INVALID_INPUT",
                          "message": str(exc), "is_future_forecast": False}
            else:
                if not clarification and on_progress:
                    on_progress("price_history")
                result = clarification or execute_tool(name, recorded_arguments, self.store)
                if result.get("status") in {"OK", "NEEDS_INPUT", "NO_DATA"}:
                    self.price_history_query = deepcopy(recorded_arguments)
                    self.price_history_revision = data_revision()
        elif name == "reconcile_settlement":
            result = execute_tool(name, arguments, self.store, shipment=self.shipment)
        elif name == "review_case_changes":
            result = execute_tool(name, arguments, self.store, reference_case=getattr(self, "comparison_reference", None),
                task_context=self.task_context.snapshot(), input_context=getattr(self, "comparison_input_context", None))
        elif name == "compare_future_pair":
            from agents.future_pair_state import AMBIGUOUS_NUMERIC_RECOVERY, is_questioned_numeric_reply, unavailable_loss_rate
            patch_values = arguments.get("patch") if isinstance(arguments, dict) else None
            pending_values = self.future_pair.pending or {}
            if (not new_comparison and pending_values.get("comparison_inputs_pending")
                    and is_questioned_numeric_reply(question)
                    and isinstance(patch_values, dict) and _has_explicit_value(patch_values)
                    and arguments.get("expected_version") == self.future_pair.version):
                result = {**deepcopy(self.future_pair.pending), "ambiguous_input": True,
                          "message": AMBIGUOUS_NUMERIC_RECOVERY}
                return result, arguments, recorded_arguments
            box_price = None
            if isinstance(arguments, dict) and isinstance(arguments.get("patch"), dict):
                patch = dict(arguments["patch"])
                action = arguments.get("action")
                if new_comparison or (future_pair_route and not keep_pair):
                    action = "edit" if comparison_started else "new"
                    if not basis_stated:
                        patch.pop("price_basis", None)
                elif routing.pending_value_reply:
                    # A pending clarification must never reset a confirmed comparison.
                    action = "edit"
                    if not basis_stated:
                        patch.pop("price_basis", None)
                elif keep_pair:
                    action = "edit"
                    if not basis_stated and "수수료" not in question:
                        patch.pop("price_basis", None)
                        if isinstance(arguments.get("clear_fields"), list):
                            arguments = {**arguments, "clear_fields": [
                                field for field in arguments["clear_fields"] if field != "price_basis"
                            ]}
                if quantity is not None:
                    patch["quantity"] = quantity
                if future_pair_route and not keep_pair:
                    action = "edit" if comparison_started else "new"
                if action == "edit" and changes_later_date_only(question):
                    protected = {"quantity", "start_date", "price_basis", "price_a", "option_a"}
                    patch = {field: value for field, value in patch.items() if field not in protected}
                    if isinstance(arguments.get("clear_fields"), list):
                        arguments = {**arguments, "clear_fields": [
                            field for field in arguments["clear_fields"]
                            if not isinstance(field, str) or field.partition(".")[0] not in protected
                        ]}
                if action == "edit":
                    for side in ("option_a", "option_b"):
                        update = patch.get(side)
                        if (isinstance(update, dict) and update.get("delay_days") is not None
                                and update["delay_days"] != self.future_pair.inputs[side]["delay_days"]
                                and unavailable_loss_rate(question, side)):
                            patch[side] = {**update, "loss_percent": None, "loss_confirmed_for_period": False}
                if routing.excludes_price_history:
                    if routing.existing_reference_request:
                        # Asking for the threshold alone does not revise confirmed inputs.
                        patch = {}
                        arguments = {**arguments, "clear_fields": []}
                    patch["price_mode"] = "manual"
                    patch["history_filters"] = None
                elif future_pair_route and patch.get("price_b") is None:
                    # Historical pairs are the default source unless the user opts out.
                    patch["price_mode"] = "history" if allow_price_history else "manual"
                    if not allow_price_history:
                        patch["history_filters"] = None
                elif future_pair_route and not requests_historical_pair_price(question):
                    patch["price_mode"] = "manual"
                    patch["history_filters"] = None
                elif action == "edit" and requests_manual_price(question):
                    # A clear opt-out during a pending comparison switches modes and drops filters.
                    patch["price_mode"] = "manual"
                    patch["history_filters"] = None
                arguments = {**arguments, "action": action, "patch": patch}
                if future_pair_route or routing.pending_value_reply or routing.excludes_price_history:
                    arguments["finish_turn"] = True
                recorded_arguments = arguments
            if has_box_price and isinstance(arguments, dict) and isinstance(arguments.get("patch"), dict):
                arguments, box_price = pending_box_arguments(question, arguments)
            result = execute_tool(name, arguments, self.store, future_pair=self.future_pair,
                                  on_progress=on_progress, box_price=box_price,
                                  unresolved_box_price=has_box_price and box_price is None)
            comparison_started |= result["status"] != "INVALID_INPUT"
        elif name == "update_scenario" and new_comparison and isinstance(arguments, dict) and isinstance(arguments.get("patch"), dict):
            patch = dict(arguments["patch"])
            if not basis_stated:
                patch.pop("price_basis", None)
            recorded_arguments = arguments = {**arguments, "patch": patch}
            result = execute_tool(name, arguments, self.store, start_new_comparison=not comparison_started)
            comparison_started |= result["status"] == "OK"
        elif name == "analyze_shipment_timing":
            result = execute_tool(name, arguments, self.store, future_pair=self.future_pair,
                                  on_progress=on_progress)
        elif name == "compare_shipment_options" and new_comparison and not comparison_started:
            result = {"status": "INVALID_INPUT", "message": "새 비교에 사용할 출하 조건을 먼저 확인해 주세요."}
        elif on_progress and name in {"compare_shipment_options", "explore_data", "build_review_brief",
                                           "get_historical_shipment_context", "get_market_evidence"}:
            result = execute_tool(name, arguments, self.store, on_progress=on_progress)
        else:
            result = execute_tool(name, arguments, self.store)
        routing.comparison_started = comparison_started
        return result, arguments, recorded_arguments

    def run(self, question: str, *, on_progress=None) -> dict:
        reply = self._run(question, on_progress=on_progress)
        remember_visible_reply(self.messages, question, reply)
        return reply

    def _run(self, question: str, *, on_progress=None) -> dict:
        from services.data_refresh import kst_today
        from presenters.presentation import select_results, render_dialogue, report_for
        from agents.future_pair_state import resume_pair_inputs, unavailable_cost, unavailable_pair_inputs
        settings = self.settings or Settings.load()
        if not isinstance(question, str) or not question.strip() or len(question) > 6000:
            return {"status": "INVALID_INPUT", "text": "질문은 1~6,000자로 입력하세요.", "tool_results": [], "events": [], "state": self.store.snapshot()}
        if outside_scope := scope_reply(question):
            return {"status": "OK", "text": outside_scope, "report": outside_scope,
                    "tool_results": [], "visible_results": [], "events": [],
                    "state": self.store.snapshot(),
                    "metrics": {"model_requests": 0, "dialogue_route": "outside_service_scope"}}
        if self.client is None and not settings.api_key:
            return {"status": "KEY_REQUIRED", "text": "프로젝트 .env에 OPENAI_API_KEY를 입력하세요. 직접 조건 비교와 과거 출하통계는 키 없이 사용할 수 있습니다.",
                    "tool_results": [], "events": [], "state": self.store.snapshot()}
        today = kst_today()
        new_comparison = starts_new_comparison(question)
        if new_comparison:
            self.future_pair.cancel_shipment_window()
        else:
            last = self.messages[-1] if self.messages else None
            previous_reply = last["content"] if isinstance(last, dict) and last.get("role") == "assistant" else ""
            window = self.future_pair.follow_up_shipment_window(
                question, today, previous_reply=previous_reply, on_progress=on_progress,
            )
            if window is not None:
                text, result = window
                entries = []
                if result is not None:
                    tool = "analyze_shipment_timing" if result["analysis_type"] == "shipment_timing" else "compare_future_pair"
                    entries = [{"tool": tool, "result": result}]
                    if tool == "analyze_shipment_timing":
                        text = render_dialogue(entries, question=question)
                return {"status": "OK", "text": text, "report": report_for(entries) if entries else text,
                        "tool_results": entries, "visible_results": entries, "events": [],
                        "state": self.store.snapshot(),
                        "metrics": {"model_requests": 0, "dialogue_route": "shipment_window"}}
        if not new_comparison and (box_result := self.future_pair.follow_up_box_price(question, on_progress=on_progress)) is not None:
            entry = {"tool": "compare_future_pair", "result": box_result}
            return {"status": "OK", "text": render_dialogue([entry], question=question,
                                                           pair_costs_deferred=self.future_pair.costs_deferred),
                    "report": report_for([entry]), "tool_results": [entry], "visible_results": [entry],
                    "events": [], "state": self.store.snapshot(), "metrics": {"model_requests": 0}}
        from agents.future_pair_state import (
            is_bare_unknown_reply, is_input_burden_complaint, paused_pair_feedback,
        )
        pending = self.future_pair.pending
        if (not new_comparison and pending is not None
                and pending.get("status") == "NEEDS_INPUT"
                and is_input_burden_complaint(question)):
            # A complaint about the number of fields is a stop signal, not another
            # invitation to advance the clarification sequence.
            self.future_pair.costs_deferred = True
            self.future_pair.specs_deferred = True
            text = paused_pair_feedback(pending)
            return {"status": "OK", "text": text, "report": text,
                    "tool_results": [], "visible_results": [], "events": [],
                    "state": self.store.snapshot(),
                    "metrics": {"model_requests": 0, "dialogue_route": "input_burden_pause"}}
        if (not new_comparison and pending is not None
                and pending.get("source_status") == "NEEDS_CLARIFICATION"
                and is_bare_unknown_reply(question)):
            # No tools or model needed to preserve state and offer a manual path.
            self.future_pair.specs_deferred = True
            entry = {"tool": "compare_future_pair", "result": deepcopy(pending)}
            return {"status": "OK", "text": render_dialogue(
                        [entry], question=question, pair_specs_deferred=True),
                    "report": report_for([entry]), "tool_results": [entry],
                    "visible_results": [entry], "events": [],
                    "state": self.store.snapshot(), "metrics": {"model_requests": 0}}
        if not new_comparison:
            direct = self.future_pair.apply_short_answer(question, on_progress=on_progress)
            if direct is not None:
                entry = {"tool": "compare_future_pair", "result": direct}
                return {"status": "OK", "text": render_dialogue([entry], question=question),
                        "report": report_for([entry]), "tool_results": [entry],
                        "visible_results": [entry], "events": [{"tool": "compare_future_pair",
                            "status": direct["status"], "origin": "verified_pending_answer",
                            "future_pair_version": self.future_pair.version}],
                        "state": self.store.snapshot(),
                        "metrics": {"model_requests": 0, "dialogue_route": "verified_pending_answer"}}
        if self.client is None:
            import_started = time.monotonic()
            OpenAI = load_model_sdk()
            log_latency("model_sdk_import", import_started)
            client_started = time.monotonic()
            self.client = OpenAI(api_key=settings.api_key, timeout=30, max_retries=0)
            log_latency("model_client_setup", client_started)
        # Every new user turn includes authoritative Python state. Old model text is not state.
        self.messages.append({"role": "user", "content": ""})
        results, events = [], []
        started = time.monotonic()
        status, calls = "OK", 0
        run_id = uuid4().hex
        metrics = {"model_requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "request_seconds": [],
                   "request_input_characters": [], "trimmed_history_turns": 0,
                   "tools_offered_per_request": [], "instruction_chars_per_request": [],
                   "focused_toolset": False, "no_tool_model_responses": 0}
        phase = "context"
        pending_model_reply = None
        guard = RepeatGuard(self.policy.identical_calls)
        source_revision = data_revision()
        if self.price_history_query is not None and self.price_history_revision != source_revision:
            self.price_history_query = None
            self.price_history_revision = None
        offered_tools, instructions = [*TOOLS, TIMING_TOOL], SYSTEM_PROMPT
        future_pair_route = is_future_pair_request(question, today)
        basis_stated = states_price_basis(question)
        pair_inputs = self.future_pair.inputs
        keep_pair = not new_comparison and pair_inputs["price_a"] is not None and (
            continues_comparison(question) or changes_later_date_only(question)
        )
        excludes_price_history = forbids_historical_pair_price(question)
        compact = "".join(question.split())
        existing_reference_request = (
            excludes_price_history and not new_comparison and not future_pair_route
            and any(phrase in compact for phrase in ("손익분기만", "손익분기단가만", "필요한단가만"))
            and all(pair_inputs[side]["delay_days"] is not None for side in ("option_a", "option_b"))
        )
        keep_pair |= existing_reference_request
        has_box_price = mentions_box_price(question)
        quantity = explicit_shipment_quantity(question) if future_pair_route else None
        explicit_history = requests_historical_pair_price(question)
        allow_price_history = (future_pair_route and not excludes_price_history
                               and (explicit_history or not requests_manual_price(question)))
        pending_value_reply = (not new_comparison and not future_pair_route
                               and self.future_pair.is_compound_value_answer(question))
        if pending_value_reply:
            instructions += ("\n이번 입력이 현재 A/B 비교의 조건 보완 답변이라면 compare_future_pair를 사용하세요. "
                             "기존 조건을 유지하고 action=edit로 명시한 값만 전달하세요. "
                             "expected_version은 현재 미래 출하 비교 상태의 version입니다. "
                             "값을 반영한 후 finish_turn=true로 종료하세요.")
        if future_pair_route:
            instructions += (
                "\n서로 다른 두 미래 날짜가 언급됐습니다. 제주 감귤 출하 비교가 맞다면 compare_future_pair를 사용하고 총 물량은 kg으로 반영하세요. "
                "'며칠 뒤'의 기준일이 따로 없으면 위 오늘 날짜를 사용하고, 달력 날짜는 보관 시작일과 구분하세요. "
                "명시된 날짜·기간·단가를 사용하고 불분명한 값만 확인하세요. "
                "B안 단가를 사용자가 직접 정하지 않았고 과거 가격 제외 요청도 없으면 price_mode=history로 자료를 먼저 확인하세요. "
                "과거 가격을 명시적으로 제외했거나 B안을 직접 입력하라고 한 경우에만 manual로 두세요. "
                "누락된 거래규격은 실제 자료 후보를 제시해 한 번에 확인하고, 임의 규격을 선택하지 마세요. "
                "과거 가격쌍 간격은 B 출하일−A 출하일입니다. 과거 사례를 미래 예측 확률이나 검증된 10일 가격 전망처럼 말하지 마세요."
            )
        if excludes_price_history and (future_pair_route or existing_reference_request):
            instructions += (
                "\n현재 제주 감귤 비교의 손익분기 요청이라면 compare_future_pair를 사용하세요. 과거 가격을 조회하거나 재사용하지 마세요. "
                "B안 단가를 명시하지 않았다면 비워 두고 이미 확인한 조건으로 참고 손익분기를 계산하세요."
            )
        if new_comparison:
            instructions += (
                "\n이번 요청은 새 출하 비교입니다. 이번 메시지에서 확인된 조건으로 시작하세요. "
                "두 미래 출하안은 action=new, 오늘과 나중은 update_scenario를 호출하세요. "
                "수수료율이 있어도 수수료 차감 전·후 기준이 명시되지 않았다면 price_basis는 null입니다."
            )
        if has_box_price:
            offered_tools = [with_box_price(tool) if tool["name"] == "compare_future_pair" else tool
                             for tool in offered_tools]
            instructions += (
                "\n두 미래 출하일의 상자당 단가에는 box_price를 사용하세요. phrase에 상자당 가격 부분의 원문을 그대로 복사하고 "
                "side에는 해당하는 A 또는 B를 넣으세요. 어느 안인지 불분명하면 null입니다. "
                "상자 가격을 kg 단가로 넣지 마세요. 환산용 무게와 과거 조회 규격을 사용자가 따로 지정하면 "
                "history_filters에는 조회 규격만 넣으세요. 나머지 명시한 조건은 함께 저장하세요."
            )
        from agents.tool_policy import select_offered_tools
        offered_tools, focused = select_offered_tools(
            offered_tools, enabled=self.focused_tools,
            future_pair_request=future_pair_route,
        )
        metrics["focused_toolset"] = focused
        routing = _TurnRouting(
            new_comparison=new_comparison,
            basis_stated=basis_stated,
            keep_pair=keep_pair,
            has_box_price=has_box_price,
            quantity=quantity,
            allow_price_history=allow_price_history,
            excludes_price_history=excludes_price_history,
            existing_reference_request=existing_reference_request,
            future_pair_route=future_pair_route,
            pending_value_reply=pending_value_reply,
        )
        start_context_version = self.task_context.snapshot()["version"]
        try:
            for _ in range(self.policy.model_requests):
                phase = "context"
                remaining = self.policy.seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise RunStopped("TIME_LIMIT")
                latest_user = user_starts(self.messages)[-1]
                self.task_context.reconcile_scenario(self.store.scenario)
                starting = (new_comparison or (future_pair_route and not keep_pair)) and not routing.comparison_started
                scenario = ({"version": self.store.version, "scenario": empty_scenario(), "provenance": {}}
                            if starting else self.store.snapshot())
                self.messages[latest_user]["content"] = (
                    f"현재 확인된 조건: {json.dumps(scenario, ensure_ascii=False)}\n"
                    f"이번 검토의 과업 맥락(선택 기록·현재 수치 아님): {json.dumps(self.task_context.snapshot(), ensure_ascii=False)}\n"
                    f"이전 과거 경락가격 조회 인수(자동 적용값·후보 제외): {json.dumps(self.price_history_query, ensure_ascii=False)}\n"
                    f"오늘(KST): {today.isoformat()}\n"
                    f"두 미래 출하안 비교 조건: {json.dumps(self.future_pair.snapshot(start_new=starting), ensure_ascii=False)}\n"
                    f"두 미래 출하안에서 확인 중인 항목: {json.dumps([] if starting else (self.future_pair.pending or {}).get('missing_fields', []), ensure_ascii=False)}\n"
                    f"이번 A/B 비교에서 직전에 사용자에게 물은 항목: {json.dumps(None if starting else self.future_pair.waiting_field(), ensure_ascii=False)}\n"
                    f"정산 기록: {json.dumps({'available': bool(self.shipment and self.shipment.revision), 'revision': self.shipment.revision if self.shipment else None, 'details': 'local_only'}, ensure_ascii=False)}\n"
                    f"사용자: {question.strip()}")
                metrics["trimmed_history_turns"] += bound_history(self.messages, self.policy.history_turns, self.policy.input_characters)
                model_messages = self.messages[user_starts(self.messages)[-1]:] if new_comparison else self.messages
                metrics["request_input_characters"].append(encoded_size(model_messages))
                metrics["tools_offered_per_request"].append(len(offered_tools))
                metrics["instruction_chars_per_request"].append(len(instructions))
                phase = "model_request"
                if on_progress and results:
                    on_progress("response")
                metrics["model_requests"] += 1
                request_started = time.monotonic()
                client = self.client.with_options(timeout=min(30, remaining), max_retries=0) if hasattr(self.client, "with_options") else self.client
                try:
                    response = client.responses.create(
                        model=settings.model, instructions=instructions, input=deepcopy(model_messages),
                        tools=offered_tools,
                        tool_choice="auto",
                        parallel_tool_calls=False,
                        store=False, max_output_tokens=4000,
                    )
                finally:
                    log_latency("model_request", request_started, call=metrics["model_requests"])
                metrics["request_seconds"].append(round(time.monotonic() - request_started, 3))
                phase = "model_output"
                usage = getattr(response, "usage", None)
                if usage:
                    metrics["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
                    metrics["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
                    details = getattr(usage, "input_tokens_details", None)
                    metrics["cached_input_tokens"] += getattr(details, "cached_tokens", 0) or 0
                if getattr(response, "model", None):
                    metrics["response_model"] = response.model
                if getattr(response, "status", "completed") == "incomplete":
                    status = "MODEL_INCOMPLETE"
                    break
                if time.monotonic() - started >= self.policy.seconds:
                    raise RunStopped("TIME_LIMIT")
                tool_calls = [item for item in response.output if item.type == "function_call"]
                if not tool_calls:
                    metrics["no_tool_model_responses"] += 1
                    candidate = getattr(response, "output_text", None)
                    if (isinstance(candidate, str) and candidate.strip()
                            and not has_unsupported_amount(candidate, model_messages)):
                        pending_model_reply = candidate.strip()
                        self.messages.extend(response.output)
                    else:
                        # Keep reasoning protocol, but never replay rejected claims as facts.
                        self.messages.extend(item for item in response.output if item.type != "message")
                        metrics["unsupported_answer_blocked"] = bool(candidate)
                    break
                self.messages.extend(response.output)
                finished = False
                for call in tool_calls:
                    phase = "tool_execution"
                    if time.monotonic() - started >= self.policy.seconds:
                        raise RunStopped("TIME_LIMIT")
                    if calls >= self.policy.tool_calls:
                        raise RunStopped("TOOL_LIMIT")
                    try:
                        arguments = json.loads(call.arguments)
                    except (TypeError, json.JSONDecodeError):
                        arguments = None
                    guard.check(call.name, arguments, self.store.scenario, data_revision())
                    calls += 1
                    tool_started = time.monotonic()
                    self.store.origin_turn_id = run_id
                    recorded_arguments = arguments
                    result, arguments, recorded_arguments = self._execute_requested_tool(
                        call.name, arguments, question, routing, on_progress=on_progress)
                    if call.name in {"update_scenario", "compare_shipment_options"} and result["status"] != "INVALID_INPUT":
                        self.future_pair.pending = None
                    self.task_context.record(call.name, recorded_arguments or {}, result, scenario=self.store.scenario)
                    item = {"tool": call.name, "result": result}
                    results.append(item)
                    events.append({"tool": call.name, "status": result["status"], "scenario_version": self.store.version,
                                   "task_context_version": self.task_context.snapshot()["version"],
                                   "elapsed_seconds": round(time.monotonic() - tool_started, 3)})
                    log_latency("tool_execution", tool_started, tool=call.name, status=result["status"])
                    model_output = model_receipt_for_tool(call.name, result)
                    self.messages.append({"type": "function_call_output", "call_id": call.call_id,
                                          "output": json.dumps(model_output, ensure_ascii=False)})
                    if time.monotonic() - started >= self.policy.seconds:
                        raise RunStopped("TIME_LIMIT")
                    if result.get("source_status") == "PACKAGE_WEIGHT_CONFLICT":
                        finished = True
                    elif isinstance(arguments, dict) and arguments.get("finish_turn") is True and (
                            result["status"] in {"OK", "NEEDS_INPUT", "NEEDS_CLARIFICATION", "NO_DATA", "PARTIAL", "CHECK_REQUIRED"}
                            or (call.name == "get_price_history" and result["status"] == "DATA_ERROR")
                            or call.name == "compare_future_pair"):
                        finished = True
                if finished:
                    break
            else:
                status = "LIMIT_REACHED"
        except Exception as exc:
            # Never expose the SDK exception, request, header, or key.
            status = classify_failure(exc, phase)
            events.append({"error_type": type(exc).__name__, "status": status, "phase": phase})
            # Drop a potentially incomplete provider conversation, keeping atomic
            # Python state and verified results for the next retry.
        if status != "OK":
            starts = user_starts(self.messages)
            if starts:
                del self.messages[starts[-1]:]
        elif new_comparison and routing.comparison_started:
            del self.messages[:user_starts(self.messages)[-1]]
        comparisons = [x for x in results if x["tool"] == "compare_shipment_options"]
        updated = any(x["tool"] == "update_scenario" and x["result"]["status"] == "OK" for x in results)
        if (comparisons and comparisons[-1]["result"].get("scenario_version") != self.store.version) or (
                updated and not comparisons and not any(x["tool"] in {"compare_sensitivity", "build_review_brief", "explain_decision_conditions", "solve_offer_limits"}
                    and x["result"].get("scenario_version") == self.store.version for x in results)):
            if on_progress:
                on_progress("compare")
            refreshed = execute_tool("compare_shipment_options", {"expected_version": self.store.version}, self.store)
            self.task_context.record("compare_shipment_options", {}, refreshed, set_goal=False, scenario=self.store.scenario)
            results.append({"tool": "compare_shipment_options", "result": refreshed})
            events.append({"tool": "compare_shipment_options", "status": refreshed["status"],
                           "scenario_version": self.store.version, "origin": "application_refresh"})
        visible = select_results(results, self.store.version)
        if self.future_pair.pending is not None and any(
            entry["tool"] == "compare_future_pair" for entry in visible
        ):
            if resume_pair_inputs(question):
                self.future_pair.costs_deferred = False
                self.future_pair.specs_deferred = False
            if unavailable_cost(question):
                self.future_pair.costs_deferred = True
            pending = self.future_pair.pending
            if pending.get("source_status") == "NEEDS_CLARIFICATION" and unavailable_pair_inputs(pending, question):
                self.future_pair.specs_deferred = True
        if pending_model_reply:
            text = pending_model_reply
            if visible:
                text += "\n\n" + render_dialogue(visible, question=question,
                    pair_costs_deferred=self.future_pair.costs_deferred,
                    pair_specs_deferred=self.future_pair.specs_deferred)
        elif visible:
            text = render_dialogue(visible, question=question, pair_costs_deferred=self.future_pair.costs_deferred,
                                   pair_specs_deferred=self.future_pair.specs_deferred)
        else:
            text = "아직 확인된 자료로 답변을 완성하지 못했습니다. 요청하신 내용으로 다시 시도해 주세요."
        report = report_for(visible) if visible else text
        if status != "OK":
            warning = "\n\n" + FAILURE_GUIDANCE.get(status, FAILURE_GUIDANCE["API_ERROR"]) + " 확인된 도구 결과까지만 부분 결과이며 요청 전체가 완료된 것은 아닙니다."
            text += warning
            report += warning
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return {"status": status, "text": text, "report": report, "visible_results": visible,
                "tool_results": results, "events": events, "run_id": run_id,
                "state": self.store.snapshot(), "metrics": metrics, "task_context": self.task_context.snapshot(),
                "termination": {"reason": status, "phase": phase, "tool_calls_executed": calls,
                                "task_context_start_version": start_context_version,
                                "source_revision": source_revision, "source_changed": source_revision != data_revision()}}
