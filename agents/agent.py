"""A bounded Responses loop with tool-grounded facts and natural dialogue."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
import time
from copy import deepcopy
from uuid import uuid4
from core.config import Settings
from agents.box_price import mentions_box_price, pending_box_arguments, standalone_box_price
from agents.candidate_storage_billing import asks_storage_mode_hypothetically, storage_mode_correction
from agents.future_pair_intent import (
    changes_later_date_only, continues_comparison, dated_storage_periods, explicit_later_storage_period, explicit_shipment_quantity, forbids_historical_pair_price,
    is_future_pair_request, requests_break_even_without_later_quote, requests_manual_price,
    requests_market_threshold_observation,
    starts_new_comparison, states_price_basis, requests_historical_pair_price,
)
from core.latency import log_latency
from agents.state import ScenarioStore, empty_scenario
from agents.tools import TOOLS, TIMING_TOOL, execute_tool, with_box_price, model_receipt_for_tool
from services.task_context import TaskContext, data_revision
from agents.harness import ExecutionPolicy, RunStopped, RepeatGuard, bound_history, encoded_size, user_starts, classify_failure, FAILURE_GUIDANCE


from agents.dialogue import explicit_term_topic, model_mentions_money, remember_visible_reply, simple_term_topic
from agents.condition_policy import (
    MUTATING_TOOLS, UNCHANGED_NOTICE, claims_state_was_changed, condition_request,
    requests_zero_for_unknown_cost, split_term_question,
)
from agents.evidence_guard import latest_lookup, lookup_revision, rejected_evidence_claim
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
    dated_storage: tuple[date, int, int] | None = None
    allow_condition_writes: bool = True
    comparison_started: bool = False
    pending_value_reply: bool = False
    price_history_cache: dict = field(default_factory=dict)
    reused_price_history: bool = False


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
        self.lookup_evidence = None
        self.shipment_variety = None
        self.shipment_candidates = None
        from agents.future_pair_state import FuturePairState
        self.future_pair = FuturePairState()

    def current_shipment_candidates(self):
        draft = self.shipment_candidates
        if draft is not None and (draft.pair_version, draft.scenario_version) == (self.future_pair.version, self.store.version):
            return draft
        return None

    def _execute_requested_tool(self, name: str, arguments, question: str, routing: _TurnRouting,
                                *, on_progress=None):
        """The sole session-aware dispatch point for stateful tool calls.

        Preserve confirmed state, tool revisions and the existing execution
        order. Deliberately do not decide the next question or format results.
        """
        if name in MUTATING_TOOLS and not routing.allow_condition_writes:
            result = {"status": "INVALID_INPUT", "reason": "READ_ONLY_REQUEST", "message": UNCHANGED_NOTICE}
            return result, arguments, arguments
        new_comparison = routing.new_comparison
        comparison_started = routing.comparison_started
        basis_stated = routing.basis_stated
        keep_pair = routing.keep_pair
        has_box_price = routing.has_box_price
        quantity = routing.quantity
        allow_price_history = routing.allow_price_history
        future_pair_route = routing.future_pair_route
        recorded_arguments = arguments
        if self.future_pair.box_price is not None and not new_comparison and (
            name in {"update_scenario", "compare_shipment_options"}
            or (name == "compare_future_pair" and not has_box_price)
        ):
            # An unrecognized reply cannot approve a quote by changing comparison tools.
            result = self.future_pair.box_confirmation()
            if name == "compare_shipment_options":
                result["scenario_version"] = self.store.version
            return result, {"finish_turn": True}, recorded_arguments
        if name == "get_price_history":
            from services.price_history_followup import (
                MissingPreviousPriceQuery, price_history_query_key, resolve_price_history_request,
            )
            try:
                recorded_arguments, clarification = resolve_price_history_request(
                    arguments, self.price_history_query, question=question,
                )
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
                if clarification:
                    result = clarification
                else:
                    from services.future_pair import price_revision

                    revision = price_revision()
                    key = (revision, price_history_query_key(recorded_arguments))
                    if key in routing.price_history_cache:
                        routing.reused_price_history = True
                        return routing.price_history_cache[key], arguments, recorded_arguments
                    if on_progress:
                        on_progress("price_history")
                    result = execute_tool(name, recorded_arguments, self.store)
                    if (revision is not None and result.get("status") in {"OK", "NEEDS_INPUT", "NO_DATA"}
                            and price_revision() == revision):
                        routing.price_history_cache[key] = result
                if result.get("status") in {"OK", "NEEDS_INPUT", "NO_DATA"}:
                    self.price_history_query = deepcopy(recorded_arguments)
                    self.price_history_revision = data_revision()
        elif name == "reconcile_settlement":
            result = execute_tool(name, arguments, self.store, shipment=self.shipment)
        elif name == "review_case_changes":
            result = execute_tool(name, arguments, self.store, reference_case=getattr(self, "comparison_reference", None),
                task_context=self.task_context.snapshot(), input_context=getattr(self, "comparison_input_context", None))
        elif name == "compare_future_pair":
            from agents.future_pair_intent import stated_storage_range
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
                if routing.dated_storage:
                    start, earlier, later = routing.dated_storage
                    patch["start_date"] = start.isoformat()
                    for side, days in (("option_a", earlier), ("option_b", later)):
                        supplied = patch.get(side)
                        if supplied is None or isinstance(supplied, dict):
                            patch[side] = {**(supplied or {}), "delay_days": days}
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
                    has_confirmed_b = (keep_pair and self.future_pair.inputs["price_b"] is not None
                                       and not requests_historical_pair_price(question))
                    patch["price_mode"] = "history" if allow_price_history and not has_confirmed_b else "manual"
                    if patch["price_mode"] == "manual":
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
                                  unresolved_box_price=has_box_price and box_price is None,
                                  storage_range=stated_storage_range(question))
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
        request, allow_writes, term_topic = question, True, None
        if isinstance(question, str) and len(question) <= 6000:
            edit, topic = split_term_question(question)
            if topic:
                draft = self.current_shipment_candidates()
                if (self._pair_storage_edit_period(edit) is not None
                        or (draft is not None and draft.storage_billing and storage_mode_correction(edit))):
                    request, term_topic = edit, topic
            request, allow_writes = condition_request(request)
        if not self.messages or (allow_writes and isinstance(request, str) and starts_new_comparison(request)):
            self.lookup_evidence = None
        reply = self._run(request, allow_condition_writes=allow_writes, on_progress=on_progress)
        if term_topic and reply.get("metrics", {}).get("dialogue_route") in {"later_storage_edit", "shipment_candidate_condition"}:
            from presenters.presentation import report_for

            term = {"tool": "explain_term", "result": execute_tool("explain_term", {"topic": term_topic}, self.store)}
            reply["text"] = term["result"]["explanation"] + "\n\n" + reply["text"]
            reply["tool_results"] = [term, *reply["tool_results"]]
            reply["visible_results"] = [term, *reply["visible_results"]]
            reply["report"] = report_for(reply["visible_results"])
            request = question
        self.remember_lookup(reply.get("visible_results", []))
        remember_visible_reply(self.messages, request, reply)
        return reply

    def _pair_storage_edit_period(self, question: str) -> int | None:
        pair = self.future_pair
        if (pair.inputs["start_date"] and pair.box_price is None
                and all(pair.inputs[option]["delay_days"] is not None for option in ("option_a", "option_b"))):
            return explicit_later_storage_period(question)
        return None

    def remember_lookup(self, entries):
        if evidence := latest_lookup(entries):
            self.lookup_evidence = (lookup_revision(evidence), deepcopy(evidence))

    def _shipment_lookup_reply(self, question, arguments, route, *, prefix="", on_progress=None):
        from presenters.presentation import render_dialogue, report_for

        result = execute_tool("analyze_shipment_timing", arguments, self.store,
                              future_pair=self.future_pair, on_progress=on_progress)
        entry = {"tool": "analyze_shipment_timing", "result": result}
        self.task_context.record(entry["tool"], arguments, result, scenario=self.store.scenario)
        text = render_dialogue([entry], question=question)
        return {"status": "OK", "text": prefix + text,
                "report": report_for([entry]), "tool_results": [entry], "visible_results": [entry],
                "events": [], "state": self.store.snapshot(),
                "metrics": {"model_requests": 0, "dialogue_route": route}}

    def _candidate_timing_reply(self, draft, question, *, on_progress=None):
        request = draft.timing_request(question)
        if isinstance(request, str):
            return self._local_reply(request, "candidate_shipment_timing")
        if request is not None:
            reply = self._shipment_lookup_reply(question, request, "candidate_shipment_timing",
                                               on_progress=on_progress)
            if not draft.has_quote():
                reply["text"] += "\n\n견적을 받으면 날짜와 kg당 금액을 알려주세요. 이 비교를 이어갈게요."
            return reply
        return None

    def _same_spec_history_reply(self, intervals, *, on_progress=None):
        from presenters.presentation import render_dialogue, report_for
        from services.future_pair import price_revision

        revision = price_revision()
        results = []
        for days in intervals:
            query = {**self.price_history_query, "interval_days": days}
            if on_progress:
                on_progress("price_history")
            result = execute_tool("get_price_history", query, self.store)
            results.append((query, {"tool": "get_price_history", "result": result}))
        route = "same_spec_history_intervals"
        if revision != price_revision():
            return self._local_reply("조회 중 자료가 바뀌었어요. 같은 규격으로 다시 조회해 주세요.", route)
        for query, entry in results:
            result = entry["result"]
            self.task_context.record(entry["tool"], query, result, scenario=self.store.scenario)
            if result["status"] in {"OK", "NEEDS_INPUT", "NO_DATA"}:
                self.price_history_query = query
                self.price_history_revision = data_revision()
        entries = [entry for _, entry in results]
        return {**self._local_reply(render_dialogue(entries), route), "report": report_for(entries),
                "tool_results": entries, "visible_results": entries}

    def _continue_with_shipment_volume(self, question, earlier, later, *, unknown_price=False, on_progress=None):
        pair = self.future_pair
        pair.opening_field = None
        if pair.pending is not None:
            pair.costs_deferred = True
        prefix = ("나중 출하 단가는 비워둘게요." if unknown_price else
                  "감모율과 비용 중 모르는 값은 미확인으로 둘게요.")
        if earlier and later and self.shipment_variety:
            return self._shipment_lookup_reply(question, {
                "shipment_variety": self.shipment_variety, "option_a_date": earlier, "option_b_date": later,
            }, "known_shipment_variety", prefix=prefix + "\n\n", on_progress=on_progress)
        pair.shipment_lookup_version = pair.version if earlier and later else None
        next_step = ("같은 날짜의 과거 출하량은 먼저 볼 수 있어요. 감귤 품종을 알려주시겠어요?" if earlier and later else
                     "과거 출하량부터 살펴볼 수 있어요. 비교할 두 출하 날짜를 알려주세요.")
        return self._local_reply(prefix + " " + next_step, "unknown_cost_recovery")

    def _local_reply(self, text, route):
        return {"status": "OK", "text": text, "report": text,
                "tool_results": [], "visible_results": [], "events": [],
                "state": self.store.snapshot(),
                "metrics": {"model_requests": 0, "dialogue_route": route}}

    def _break_even_reply(self, question):
        from presenters.presentation import render_dialogue, report_for

        result = self.future_pair.continue_without_later_quote()
        entry = {"tool": "compare_future_pair", "result": result}
        text = render_dialogue([entry], question=question if result["status"] != "NEEDS_INPUT" else "",
                               pair_one_question=True)
        return {**self._local_reply(text, "later_quote_missing"), "report": report_for([entry]),
                "market_observation_excluded": (forbids_historical_pair_price(question)
                                                or requests_manual_price(question)
                                                or question.startswith("과거 자료 없이")),
                "tool_results": [entry], "visible_results": [entry]}

    def _market_threshold_reply(self, current):
        from services.market_decision import threshold_observation_eligible

        if not threshold_observation_eligible(current):
            text = ("수수료를 빼기 전 손익분기 단가와 비용을 확인하면 같은 기준의 과거 경락가격과 대조할 수 있어요.")
            return self._local_reply(text, "market_observation_unavailable")
        text = "아래에서 같은 거래규격을 확인해 주세요. 과거 같은 시기에 손익분기 가격 이상이었던 날을 세어드릴게요."
        entry = {"tool": "compare_future_pair", "result": current}
        return {**self._local_reply(text, "market_observation_request"),
                "visible_results": [entry], "market_observation_requested": True}

    def _run(self, question: str, *, allow_condition_writes=True, on_progress=None) -> dict:
        from services.data_refresh import kst_today
        from presenters.presentation import select_results, render_dialogue, report_for
        from agents.future_pair_intent import dated_shipment_query
        from agents.future_pair_state import (
            LABELS, asks_where_to_check, is_bare_unknown_reply, mentions_unknown_cost,
            next_pair_field, resume_pair_inputs, shipment_variety_reply, unavailable_cost, unavailable_pair_inputs,
        )
        from services.shipment_timing import CATEGORIES, resolve_pair_dates
        settings = self.settings or Settings.load()
        if not isinstance(question, str) or not question.strip() or len(question) > 6000:
            return {"status": "INVALID_INPUT", "text": "질문은 1~6,000자로 입력하세요.", "tool_results": [], "events": [], "state": self.store.snapshot()}
        if outside_scope := scope_reply(question):
            return {"status": "OK", "text": outside_scope, "report": outside_scope,
                    "tool_results": [], "visible_results": [], "events": [],
                    "state": self.store.snapshot(),
                    "metrics": {"model_requests": 0, "dialogue_route": "outside_service_scope"}}
        today = kst_today()
        pair = self.future_pair
        if self.price_history_query and self.price_history_revision == data_revision():
            from services.price_history_followup import requested_same_spec_intervals

            if intervals := requested_same_spec_intervals(question):
                return self._same_spec_history_reply(intervals, on_progress=on_progress)
        draft = self.current_shipment_candidates()
        if draft is not None and draft.cancel_quote(question):
            return self._local_reply("견적은 그대로 둘게요.", "shipment_candidate_quote")
        if draft is not None and draft.storage_billing and asks_storage_mode_hypothetically(question):
            return self._local_reply("현재 청구 방식은 그대로 뒀어요. 실제로 적용할 방식이 확인되면 알려주세요.",
                                     "read_only_conditions")
        candidate_term = explicit_term_topic(question) if draft is not None and (
            draft.pending_quote is not None or draft.next_condition() is not None or draft.storage_pause is not None
            or draft.timing_dates is not None or draft.storage_billing is not None
        ) else None
        if candidate_term:
            allow_condition_writes = False
        new_comparison = allow_condition_writes and starts_new_comparison(question)
        if allow_condition_writes:
            from agents.shipment_candidates import open_candidate_draft, requested_candidate_dates

            selected = requested_candidate_dates(question, today)
            if selected is not None:
                dates, start = selected
                if pair.box_price is not None:
                    pair.clear_result()
                draft = open_candidate_draft(dates, start, question, today, pair, self.store.version,
                                             new_comparison=new_comparison)
                self.shipment_candidates = draft
                if timing := self._candidate_timing_reply(draft, question, on_progress=on_progress):
                    return timing
                return self._local_reply(draft.summary(), "shipment_candidates")
            if draft is not None and not new_comparison:
                if draft.update_availability(question, today) or draft.complete_selection(question, today):
                    return self._local_reply(draft.summary(), "shipment_candidates")
        if draft is not None and allow_condition_writes and not new_comparison:
            if timing := self._candidate_timing_reply(draft, question, on_progress=on_progress):
                return timing
        if allow_condition_writes:
            if draft is not None and not new_comparison:
                if answer := draft.answer_condition(question):
                    result, unknown = answer
                    if result["status"] == "OK":
                        from presenters.reporting import shipment_candidate_amounts, shipment_candidate_summary

                        text = shipment_candidate_summary(result) + "\n\n" + shipment_candidate_amounts(result)
                    else:
                        text = result["message"]
                    if unknown and draft.storage_pause is None:
                        text = "모르는 값은 미확인으로 둘게요.\n\n" + text
                    return self._local_reply(text, "shipment_candidate_condition")
                if text := draft.update_quote(question, today, on_progress=on_progress):
                    return self._local_reply(text, "shipment_candidate_quote")
        if new_comparison:
            self.shipment_candidates = None
            pair.clear_availability()
            if pair.box_price is not None:
                pair.clear_result()
        future_pair_route = allow_condition_writes and is_future_pair_request(question, today)
        dated_storage = dated_storage_periods(question, today) if future_pair_route else None
        pending = pair.pending
        waiting_for_variety = pair.shipment_lookup_version == pair.version
        pair.shipment_lookup_version = None
        earlier, later = resolve_pair_dates(pair.inputs)
        if requests_market_threshold_observation(question) and pair.quote_deferred:
            return self._market_threshold_reply(pair.current_result())
        if new_comparison:
            self.shipment_variety = None
        variety = shipment_variety_reply(question, CATEGORIES)
        if allow_condition_writes and variety and (waiting_for_variety or not self.messages):
            self.shipment_variety = variety
        elif future_pair_route:
            named = [label for label in CATEGORIES if label in question]
            first_clause = re.split(r"[,，.!。]", question, maxsplit=1)[0]
            if len(named) == 1 and (
                shipment_variety_reply(first_clause, CATEGORIES) == named[0]
                or re.match(rf"\s*{named[0]}\s+(?:\d|감귤|출하)", first_clause)
            ):
                self.shipment_variety = named[0]
        unknown_cost_reply = (
            earlier and later and pending is not None and mentions_unknown_cost(question)
            and not re.search(r"\d", question) and not changes_later_date_only(question)
        )
        price_waiting = pair.waiting_field() == LABELS["price_b"] or bool(
            pending and pending.get("manual_price_required"))
        unknown_opening_price = price_waiting and is_bare_unknown_reply(question)
        defer_without_costs = unknown_opening_price and all(
            pair.inputs[side]["loss_percent"] is None for side in ("option_a", "option_b"))
        if (not new_comparison and not future_pair_route and not defer_without_costs
                and pair.box_price is None and earlier and later and pair.inputs["price_b"] is None
                and ((allow_condition_writes and unknown_opening_price) or requests_break_even_without_later_quote(
                    question, waiting_for_quote=price_waiting))):
            return self._break_even_reply(question)
        unanswered_break_even = (pending and pending.get("waiting_for_break_even")
                                  and is_bare_unknown_reply(question))
        if (allow_condition_writes and not new_comparison and not future_pair_route
                and (requests_zero_for_unknown_cost(question) or unknown_cost_reply
                     or unknown_opening_price or unanswered_break_even)):
            return self._continue_with_shipment_volume(question, earlier, later,
                unknown_price=unknown_opening_price, on_progress=on_progress)
        if self.client is None and not settings.api_key:
            return {"status": "KEY_REQUIRED", "text": "프로젝트 .env에 OPENAI_API_KEY를 입력하세요. 직접 조건 비교와 과거 출하통계는 키 없이 사용할 수 있습니다.",
                    "tool_results": [], "events": [], "state": self.store.snapshot()}
        if waiting_for_variety and earlier and later and variety:
            return self._shipment_lookup_reply(question, {
                "shipment_variety": variety, "option_a_date": earlier, "option_b_date": later,
            }, "pending_shipment_lookup", on_progress=on_progress)
        if allow_condition_writes and variety and not self.messages and not (earlier and later):
            return self._local_reply(f"{variety}로 확인했어요. 어느 두 날의 출하를 비교해 볼까요?", "shipment_variety_first")
        if timing_args := dated_shipment_query(question, today, CATEGORIES):
            return self._shipment_lookup_reply(question, timing_args, "dated_shipment_lookup", on_progress=on_progress)
        if allow_condition_writes:
            availability = pair.follow_up_availability(question, today, on_progress=on_progress)
            cost_range = (None if availability is not None or new_comparison else
                          pair.apply_storage_cost_range(question, on_progress=on_progress))
            if availability is not None or cost_range is not None:
                text, result = availability if availability is not None else ("", cost_range)
                entries = [{"tool": "compare_future_pair", "result": result}] if result is not None else []
                if entries and (not text or result["status"] == "OK"):
                    rendered = render_dialogue(entries, question=question, pair_one_question=True)
                    text = "\n\n".join(part for part in (text, rendered) if part)
                route = "shipment_availability" if availability is not None else "storage_cost_range"
                return {**self._local_reply(text, route), "tool_results": entries, "visible_results": entries,
                        "report": report_for(entries) if entries else text}
        if new_comparison or future_pair_route:
            self.future_pair.cancel_shipment_window()
        elif allow_condition_writes:
            last = self.messages[-1] if self.messages else None
            previous_reply = last["content"] if isinstance(last, dict) and last.get("role") == "assistant" else ""
            window = self.future_pair.follow_up_shipment_window(
                question, today, previous_reply=previous_reply, shipment_variety=self.shipment_variety,
                on_progress=on_progress,
            )
            if window is not None:
                text, result = window
                entries = []
                if result is not None:
                    tool = "analyze_shipment_timing" if result["analysis_type"] == "shipment_timing" else "compare_future_pair"
                    entries = [{"tool": tool, "result": result}]
                    if tool == "analyze_shipment_timing":
                        self.shipment_variety = result.get("input", {}).get("shipment_variety", self.shipment_variety)
                        text = render_dialogue(entries, question=question)
                return {"status": "OK", "text": text, "report": report_for(entries) if entries else text,
                        "tool_results": entries, "visible_results": entries, "events": [],
                        "state": self.store.snapshot(),
                        "metrics": {"model_requests": 0, "dialogue_route": "shipment_window"}}
        if allow_condition_writes and not new_comparison and (box_result := self.future_pair.follow_up_box_price(question, on_progress=on_progress)) is not None:
            entry = {"tool": "compare_future_pair", "result": box_result}
            return {"status": "OK", "text": render_dialogue([entry], question=question,
                                                           pair_costs_deferred=pair.costs_deferred, pair_one_question=True),
                    "report": report_for([entry]), "tool_results": [entry], "visible_results": [entry],
                    "events": [], "state": self.store.snapshot(), "metrics": {"model_requests": 0}}
        if allow_condition_writes and not (new_comparison or future_pair_route or pair.box_price):
            field = pair.waiting_field()
            expected_side = {LABELS["price_a"]: "A", LABELS["price_b"]: "B"}.get(field)
            before_schedule = not any(pair.inputs.get(key) is not None for key in ("start_date", "price_a", "price_b"))
            quote = standalone_box_price(question, expected_side=expected_side) if expected_side or before_schedule else None
            if quote is not None:
                result = pair.apply("edit", pair.version, {}, box_price=quote)
                entry = {"tool": "compare_future_pair", "result": result}
                return {**self._local_reply(result["message"], "standalone_box_quote"),
                        "tool_results": [entry], "visible_results": [entry]}
        from agents.future_pair_state import is_input_burden_complaint, paused_pair_feedback
        pending = self.future_pair.pending
        if (allow_condition_writes and not new_comparison and pending is not None
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
        if (allow_condition_writes and not new_comparison and pending is not None
                and pending.get("source_status") == "NEEDS_CLARIFICATION"
                and (is_bare_unknown_reply(question)
                     or (not re.search(r"\d", question) and asks_where_to_check(question)
                         and unavailable_pair_inputs(pending, question)))):
            # No tools or model needed to preserve state and offer a manual path.
            self.future_pair.specs_deferred = True
            entry = {"tool": "compare_future_pair", "result": deepcopy(pending)}
            return {"status": "OK", "text": render_dialogue(
                        [entry], question=question, pair_specs_deferred=True),
                    "report": report_for([entry]), "tool_results": [entry],
                    "visible_results": [entry], "events": [],
                    "state": self.store.snapshot(), "metrics": {"model_requests": 0}}
        if allow_condition_writes and not new_comparison:
            pair = self.future_pair
            later_period = self._pair_storage_edit_period(question)
            direct = None
            route = "verified_pending_answer"
            if later_period is not None:
                direct = pair.apply("edit", pair.version, {"option_b": {"delay_days": later_period}},
                                    on_progress=on_progress)
                route = "later_storage_edit"
            if direct is None:
                direct = pair.apply_explicit_pair_prices(question, on_progress=on_progress)
            if direct is None:
                direct = self.future_pair.apply_short_answer(question, on_progress=on_progress)
            if direct is not None:
                entry = {"tool": "compare_future_pair", "result": direct}
                text = render_dialogue([entry], question=question, pair_one_question=pair.quote_deferred)
                if route == "later_storage_edit" and direct["status"] != "INVALID_INPUT":
                    text = f"B안 보관일을 {later_period}일로 바꿨어요.\n\n" + text
                return {"status": "OK", "text": text,
                        "report": report_for([entry]), "tool_results": [entry],
                        "visible_results": [entry], "events": [{"tool": "compare_future_pair",
                            "status": direct["status"], "origin": route,
                            "future_pair_version": self.future_pair.version}],
                        "state": self.store.snapshot(),
                        "metrics": {"model_requests": 0, "dialogue_route": route}}
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
        evidence_fallback = None
        guard = RepeatGuard(self.policy.identical_calls)
        source_revision = data_revision()
        if self.price_history_query is not None and self.price_history_revision != source_revision:
            self.price_history_query = None
            self.price_history_revision = None
        offered_tools, instructions = [*TOOLS, TIMING_TOOL], SYSTEM_PROMPT
        if not allow_condition_writes:
            offered_tools = [tool for tool in offered_tools if tool["name"] not in MUTATING_TOOLS]
            instructions += ("\n이번 질문에는 조회와 설명 도구만 사용할 수 있습니다. "
                             "인용·가정·철회 표현에서 값을 저장하거나, 적용됐다고 말하지 마세요.")
            metrics["dialogue_route"] = "read_only_conditions"
        if candidate_term:
            offered_tools = [tool for tool in offered_tools if tool["name"] == "explain_term"]
        basis_stated = states_price_basis(question)
        pair_inputs = self.future_pair.inputs
        adds_dates_to_quote = (future_pair_route and not new_comparison and pair.pending is not None
                               and pair_inputs["start_date"] is None
                               and any(pair_inputs[key] is not None for key in ("price_a", "price_b")))
        keep_pair = not new_comparison and pair_inputs["price_a"] is not None and (
            continues_comparison(question) or changes_later_date_only(question)
        )
        keep_pair |= adds_dates_to_quote
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
                "보관 시작일을 명시했다면 '며칠 뒤'는 그날 기준입니다. 기준일이 없으면 위 오늘 날짜를 사용하세요. "
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
            dated_storage=dated_storage,
            allow_condition_writes=allow_condition_writes,
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
                tool_choice = "auto"
                if candidate_term:
                    tool_choice = {"type": "function", "name": "explain_term"}
                elif (future_pair_route and (has_box_price or explicit_history or dated_storage or adds_dates_to_quote)
                        and metrics["model_requests"] == 1):
                    tool_choice = {"type": "function", "name": "compare_future_pair"}
                try:
                    response = client.responses.create(
                        model=settings.model, instructions=instructions, input=deepcopy(model_messages),
                        tools=offered_tools,
                        tool_choice=tool_choice,
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
                    if candidate_term:
                        self.messages.extend(item for item in response.output if item.type != "message")
                        break
                    candidate = getattr(response, "output_text", None)
                    if ((not allow_condition_writes or pair.box_price is not None)
                            and isinstance(candidate, str) and claims_state_was_changed(candidate)):
                        self.messages.extend(item for item in response.output if item.type != "message")
                        pending_model_reply = UNCHANGED_NOTICE
                        metrics["condition_write_claim_blocked"] = True
                        break
                    recent_evidence = self.lookup_evidence
                    evidence = latest_lookup(results)
                    if evidence is None and recent_evidence is not None:
                        revision, previous = recent_evidence
                        evidence = (previous if revision is not None and revision == lookup_revision(previous)
                                    else {"tool": previous["tool"], "result": {"status": "STALE_SOURCE"}})
                    rejection = rejected_evidence_claim(candidate, question, evidence) if isinstance(candidate, str) else None
                    if rejection:
                        metrics["unsupported_evidence_blocked"] = rejection
                        if rejection == "historical_probability":
                            evidence_fallback = "과거 출하량이나 가격쌍의 비율로 미래 가격이 오를 확률을 계산할 수 없어요."
                        elif rejection == "missing_observation":
                            evidence_fallback = "현재 조회로 확인된 수치가 없어요. 품종과 확인할 날짜로 다시 조회할 수 있어요."
                        else:
                            evidence_fallback = "말씀하신 수치는 확인된 관측 결과와 일치하지 않아요."
                        if (rejection != "missing_observation" and not results and evidence
                                and evidence["result"].get("status") != "STALE_SOURCE"
                                and re.search(r"방금|직전|그럼|그\s*자료|그\s*수치", question)):
                            evidence_fallback += "\n\n" + render_dialogue([evidence])
                    model_money = isinstance(candidate, str) and model_mentions_money(candidate)
                    if model_money:
                        metrics["model_money_requires_tool"] = True
                    if isinstance(candidate, str) and candidate.strip() and not rejection and not model_money:
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
                    if candidate_term and call.name == "explain_term":
                        arguments = {"topic": candidate_term, "finish_turn": True}
                    logical_state = (self.future_pair.inputs if call.name in {"compare_future_pair", "analyze_shipment_timing"}
                                     else self.store.scenario)
                    guard.check(call.name, arguments, logical_state, data_revision())
                    calls += 1
                    tool_started = time.monotonic()
                    self.store.origin_turn_id = run_id
                    routing.reused_price_history = False
                    result, arguments, recorded_arguments = self._execute_requested_tool(
                        call.name, arguments, question, routing, on_progress=on_progress)
                    if (routing.allow_condition_writes and call.name in {"update_scenario", "compare_shipment_options"}
                            and result["status"] != "INVALID_INPUT"
                            and result.get("source_status") != "BOX_PRICE_CONFIRMATION"):
                        self.future_pair.pending = None
                    model_output = model_receipt_for_tool(call.name, result)
                    if routing.reused_price_history:
                        metrics.setdefault("reused_price_history_calls", []).append(str(call.call_id))
                        model_output["reused_same_turn"] = True
                    else:
                        self.task_context.record(call.name, recorded_arguments or {}, result, scenario=self.store.scenario)
                        results.append({"tool": call.name, "result": result})
                        events.append({"tool": call.name, "status": result["status"], "scenario_version": self.store.version,
                                       "task_context_version": self.task_context.snapshot()["version"],
                                       "elapsed_seconds": round(time.monotonic() - tool_started, 3)})
                        log_latency("tool_execution", tool_started, tool=call.name, status=result["status"])
                    self.messages.append({"type": "function_call_output", "call_id": call.call_id,
                                          "output": json.dumps(model_output, ensure_ascii=False)})
                    if time.monotonic() - started >= self.policy.seconds:
                        raise RunStopped("TIME_LIMIT")
                    if result.get("data_gap") or result.get("source_status") in {"PACKAGE_WEIGHT_CONFLICT", "BOX_PRICE_CONFIRMATION"}:
                        finished = True
                    elif result.get("reason") != "READ_ONLY_REQUEST" and isinstance(arguments, dict) and arguments.get("finish_turn") is True and (
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
        source_changed = source_revision != data_revision()
        visible_results = [entry for entry in results if entry["result"].get("reason") != "READ_ONLY_REQUEST"]
        if source_changed:
            visible_results = [entry for entry in visible_results if
                               entry["tool"] not in {"get_price_history", "get_price_overview", "get_market_evidence",
                                                     "get_historical_shipment_context", "analyze_shipment_timing",
                                                     "explore_data", "build_review_brief", "review_case_changes"}
                               and not (entry["tool"] == "compare_future_pair" and
                                        (entry["result"].get("history_preview")
                                         or entry["result"].get("historical_price_lookup") == "used"))]
            pending_model_reply = evidence_fallback = None
        visible = select_results(visible_results, self.store.version)
        if status == "OK" and not visible and not pending_model_reply and not evidence_fallback:
            if topic := simple_term_topic(question):
                args = {"topic": topic}
                term = {"tool": "explain_term", "result": execute_tool("explain_term", args, self.store)}
                results.append(term)
                visible = [term]
                self.task_context.record("explain_term", args, term["result"], scenario=self.store.scenario)
                metrics["dialogue_route"] = "local_term_fallback"
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
        opening = next((entry["result"] for entry in visible if entry["tool"] == "compare_future_pair"
                        and entry["result"].get("future_pair_version") == pair.version), None)
        saved_line = (pair.saved_conditions_line() if status == "OK" and future_pair_route and
                      routing.comparison_started and not keep_pair and opening else "")
        if saved_line and opening.get("status") == "NEEDS_INPUT" and pair.costs_deferred:
            recovery = self._continue_with_shipment_volume(
                question, *resolve_pair_dates(pair.inputs), on_progress=on_progress,
            )
            results.extend(recovery["tool_results"])
            visible.extend(recovery["visible_results"])
            text = saved_line + "\n\n" + recovery["text"]
        elif (saved_line and opening.get("source_status") == "NEEDS_CLARIFICATION"
              and not (explicit_history or pair.specs_deferred)):
            pair.opening_field = LABELS["price_b"]
            text = (saved_line + "\n\n나중 출하 때 받을 단가는 kg당 얼마로 제안받으셨나요? "
                    "아직 모르면 ‘몰라요’라고 해도 돼요.")
        elif saved_line:
            if opening.get("comparison_inputs_pending"):
                pair.opening_field = next_pair_field(opening.get("missing_fields", []))
            text = saved_line + "\n\n" + render_dialogue(
                visible, question=question, pair_costs_deferred=pair.costs_deferred,
                pair_specs_deferred=pair.specs_deferred, pair_one_question=True,
            )
        elif pending_model_reply or evidence_fallback:
            text = pending_model_reply or evidence_fallback
            if visible:
                text += "\n\n" + render_dialogue(visible, question=question,
                    pair_costs_deferred=self.future_pair.costs_deferred,
                    pair_specs_deferred=self.future_pair.specs_deferred)
        elif visible:
            text = render_dialogue(visible, question=question, pair_costs_deferred=self.future_pair.costs_deferred,
                                   pair_specs_deferred=self.future_pair.specs_deferred)
        elif source_changed:
            text = "조회 중 자료가 바뀌었어요. 현재 자료 기준으로 다시 조회해 주세요."
        elif not allow_condition_writes and any(entry["result"].get("reason") == "READ_ONLY_REQUEST" for entry in results):
            text = UNCHANGED_NOTICE
        elif metrics.get("model_money_requires_tool"):
            text = "이 답변에 표시할 금액을 확인하지 못했어요. 견적을 비교하거나 과거 경락가격을 조회해 확인할 수 있어요."
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
                "termination": {"reason": status, "phase": phase,
                                "tool_calls_executed": calls - len(metrics.get("reused_price_history_calls", ())),
                                "task_context_start_version": start_context_version,
                                "source_revision": source_revision, "source_changed": source_changed}}
