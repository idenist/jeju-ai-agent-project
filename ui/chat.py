"""Shared conversational/card workspace. CSV tools and PDF work without a model key."""
from __future__ import annotations

import json
import re
from time import monotonic, time
from datetime import date, timedelta
import streamlit as st

from core.latency import latency_logging_enabled, log_latency
from presenters.reporting import manual_price_warning
from presenters.presentation import render_dialogue

_REPORT_REQUEST = re.compile(
    r"(?:방금|아까|직전|마지막|최근)\s*(?:한\s*)?(?:출하\s*)?비교\s*(?:결과)?\s*(?:를|을)?\s*"
    r"(?:(?:보고서\s*(?:로|를)?\s*)?(?:pdf|피디에프)\s*(?:파일)?\s*(?:로|를)?|보고서\s*(?:로|를)?)\s*"
    r"(?:다시\s*)?(?:줘(?:요)?|주세요|보여\s*줘(?:요)?|보여\s*주세요|받을\s*수\s*있나요|다운로드)?[.!?\s]*",
    re.IGNORECASE,
)
_BASIS_CHOICES = {"빼기 전": "gross", "뺀 뒤": "commission_net", "잘 모르겠어요": None}



from ui.context import (
    _workspace,
    _preload_model_sdk,
    _card_dirty,
    _sync_card,
    _load_demo,
    _reload_cards,
    _on_period_edit,
    _clear_chat_only,
    _reset_all,
    _clear_sensitivity,
    _clear_review,
)

from ui.panels import (
    _render_cases,
    _render_case_download,
    _discovery_changed,
    _discovery_choices,
    _render_discovery,
    _render_offer_limits,
    _render_review,
    _render_task_context,
    _clear_csv_preview,
    _render_csv_manager,
    _render_report,
    _render_card_summary,
    _render_cards,
    _render_price_history,
    _render_history,
    _render_sensitivity,
)

_SHIPMENT_PROGRESS = {
    "conditions": ("출하 조건 확인 중…", "알려주신 출하 조건과 더 필요한 정보를 확인합니다."),
    "price_history": ("과거 경락가격 CSV 조회 중…", "같은 거래규격의 경락가격을 로컬 CSV에서 확인합니다."),
    "market_csv": ("시장 가격 CSV 조회 중…", "저장된 경매 API 수집본(CSV)에서 시장 가격을 확인합니다."),
    "shipment_csv": ("출하 통계 CSV 조회 중…", "로컬 CSV에서 품종과 기간에 해당하는 출하 통계를 찾아봅니다."),
    "catalog_csv": ("확보 자료 살펴보는 중…", "로컬 CSV에서 조회할 수 있는 거래규격이나 출하 통계를 찾아봅니다."),
    "response": ("확인한 자료로 답변 준비 중…", "도구에서 확인한 내용을 바탕으로 답변을 준비합니다."),
    "compare": ("감모율과 비용을 반영해 비교 중…", "입력하신 감모율과 비용을 반영해 출하 금액을 비교합니다."),
}

_SHIPMENT_STEP_DETAILS = {
    "출하 조건 확인": _SHIPMENT_PROGRESS["conditions"][1],
    "과거 가격 조회": _SHIPMENT_PROGRESS["price_history"][1],
    "도구 결과 확인": _SHIPMENT_PROGRESS["response"][1],
    "감모율과 비용을 반영해 출하 비교": _SHIPMENT_PROGRESS["compare"][1],
}

def _show_agent_reply(reply, current_version, draft_pending=False, past_future_pair=False):
    from presenters.settlement_reporting import render_settlement, citation_details
    settlements = [x['result'] for x in reply.get('visible_results', []) if x['result'].get('analysis_type') == 'settlement' and 'citations' in x['result']]
    display_text = reply.get("text", "응답을 확인할 수 없습니다.")
    for result in settlements:
        display_text = display_text.replace(render_settlement(result), render_settlement(result, details=False))
    version = reply.get("state", {}).get("version")
    if past_future_pair:
        st.caption("이 답변은 이전 비교 조건에 대한 응답입니다.")
    elif draft_pending or (version != current_version and not any(
            entry["tool"] == "compare_future_pair" for entry in reply.get("visible_results", []))):
        suffix = "카드에 저장 전 수정이 있습니다" if draft_pending else f"현재 버전 {current_version}"
        st.caption(f"이 답변은 이전 조건 버전 {version} 기준입니다 · {suffix}")
    st.markdown(display_text)
    for result in settlements:
        with st.expander(f"정산 근거 보기 · 기록 버전 {result['record_revision']}"):
            st.markdown(citation_details(result))

def _shipment_progress_state(status):
    return "complete" if status in {None, "OK", "NEEDS_INPUT", "NO_DATA", "INSUFFICIENT_DATA"} else "error"

def _shipment_work_label(elapsed):
    if elapsed is None:
        return "작업 과정"
    if elapsed < 1:
        return "1초 미만 작업함"
    minutes, seconds = divmod(int(elapsed), 60)
    duration = f"{minutes}분 {seconds}초" if minutes else f"{seconds}초"
    return f"{duration} 동안 작업함"

def _run_with_shipment_progress(run, started=None):
    started = monotonic() if started is None else started
    steps = []
    status = st.status(_SHIPMENT_PROGRESS["conditions"][0], type="compact", expanded=True)
    timer = status.empty()
    timer.html(_shipment_timer_html(started), unsafe_allow_javascript=True)

    def report(stage, *, initial=False):
        label, detail = _SHIPMENT_PROGRESS[stage]
        if not steps or steps[-1] != detail:
            steps.append(detail)
            if not initial:
                status.update(label=label)
            status.write(detail)

    report("conditions", initial=True)
    result = run(report)
    elapsed = monotonic() - started
    status.update(label=_shipment_work_label(elapsed), state=_shipment_progress_state(result["status"]), expanded=False)
    timer.empty()
    return result, steps, elapsed

def _shipment_timer_html(started):
    # This only inserts a server-measured numeric offset. The browser ticks without a rerun.
    timer_id = f"shipment-clock-{int(started * 1_000_000)}"
    elapsed_ms = max(0, int((monotonic() - started) * 1000))
    return f"""<span id="{timer_id}" hidden></span><script>
    (() => {{
        const marker = document.getElementById('{timer_id}');
        const summary = marker?.closest('details')?.querySelector('summary');
        if (!summary) return;
        const clock = document.createElement('span');
        clock.className = 'shipment-clock';
        clock.style.cssText = 'white-space:nowrap;font-variant-numeric:tabular-nums';
        clock.setAttribute('aria-hidden', 'true');
        const begin = performance.now() - {elapsed_ms};
        const tick = () => {{
            if (!marker.isConnected || !summary.isConnected) {{
                clock.remove();
                return false;
            }}
            clock.textContent = ` · ${{Math.floor((performance.now() - begin) / 1000)}}초`;
            if (!clock.isConnected) summary.firstElementChild?.append(clock);
            return true;
        }};
        if (tick()) {{
            const interval = setInterval(() => {{ if (!tick()) clearInterval(interval); }}, 200);
        }}
    }})();
    </script>"""

def _reply_to_future_pair_edit(ws, version, patch, *, on_progress=None):
    pair = ws.session.future_pair
    result = pair.apply("edit", version, patch, on_progress=on_progress)
    entry = {"tool": "compare_future_pair", "result": result}
    return {"state": ws.store.snapshot(), "status": result["status"], "visible_results": [entry],
            "text": render_dialogue([entry], pair_costs_deferred=pair.costs_deferred,
                                    pair_specs_deferred=pair.specs_deferred)}

def _render_later_shipment_edit(ws, pair, version):
    start = date.fromisoformat(pair["start_date"])
    earlier = date.fromisoformat(pair["option_a"]["sale_date"])
    later = date.fromisoformat(pair["options_b"][0]["sale_date"])
    last_day = start + timedelta(days=min(365, (date.max - start).days))
    with st.expander("나중 출하일 바꾸기"):
        st.caption(f"먼저 출하하는 {earlier}과 나머지 조건을 유지합니다. 새 보관기간의 감모율을 이어서 확인합니다.")
        with st.form(f"future_pair_chat_date_{version}"):
            changed = st.date_input("새 나중 출하일", value=later,
                                    min_value=earlier + timedelta(days=1), max_value=last_day)
            submitted = st.form_submit_button("이 날짜로 변경")
        if submitted:
            if changed is None or changed == later:
                st.info("현재와 다른 출하일을 골라주세요.")
            else:
                days = (changed - start).days
                _submit_shipment_question(f"나중 출하일만 {changed}로 변경 · {days}일 보관",
                                          pair_edit=(version, {"option_b": {"delay_days": days}}))
                st.rerun()

def _submit_shipment_question(example=None, *, timing_lookup=None, pair_edit=None, basis_choice=None):
    question = example if example is not None else st.session_state.get("shipment_question", "")
    messages = st.session_state.setdefault("agent_messages", [])
    if question.strip() and not (messages and messages[-1].get("pending")):
        messages.append({"role": "user", "content": question.strip(), "pending": True, "started": monotonic(),
                         "timing_lookup": timing_lookup, "pair_edit": pair_edit, "basis_choice": basis_choice})
        if latency_logging_enabled():
            messages[-1]["submitted_ms"] = int(time() * 1000)

def _render_shipment_composer(disabled, show_examples, *, busy=False):
    if show_examples:
        examples = (
            ("지금 팔지 더 보관할지 비교", "감귤을 지금 팔 때와 더 보관하고 팔 때를 비교해 주세요."),
            ("두 출하일 비교 시작", "두 미래 출하일에 감귤을 팔 때를 비교해 주세요."),
        )
        with st.container(key="shipment_examples", horizontal=True, gap="small"):
            for label, example in examples:
                st.button(label, disabled=disabled, on_click=_submit_shipment_question, args=(example,))
    suffix = "_busy" if busy else ""
    with st.container(key=f"shipment_composer{suffix}"):
        with st.form(f"shipment_chat{suffix}", clear_on_submit=True, border=False):
            field, action = st.columns([5, 1], vertical_alignment="bottom")
            field.text_input(
                "출하 고민을 입력하세요",
                key=f"shipment_question{suffix}",
                placeholder="예: 감귤 3톤, 언제 팔까요?",
                disabled=disabled,
            )
            action.form_submit_button(
                "보내기", key=f"shipment_send{suffix}", type="primary", disabled=disabled, width="stretch",
                on_click=_submit_shipment_question,
            )

def _render_chat_timing_action(version, earlier, later):
    from services.shipment_timing import CATEGORIES

    st.caption("보조 자료 · 제주데이터허브의 과거 신고 출하량입니다. 품종과 날짜가 맞는 기록만 확인하며, 현재 공급량이나 최적 출하일을 뜻하지 않습니다.")
    with st.expander("같은 날짜의 과거 출하량 보기"):
        st.caption(f"{earlier} · {later} 출하 기준")
        with st.form(f"chat_timing_{version}", border=False):
            variety = st.selectbox("출하통계 품종", list(CATEGORIES), index=None,
                                   key=f"chat_timing_variety_{version}",
                                   placeholder="품종을 선택하세요",
                                   help="제주데이터허브 출하통계에 기록된 품종을 선택해 주세요.")
            submitted = st.form_submit_button("과거 출하량 조회")
        if submitted and variety is None:
            st.warning("출하통계 품종을 선택해 주세요.")
        elif submitted:
            _submit_shipment_question(f"{variety}의 같은 날짜 과거 출하량을 비교해 주세요.",
                                      timing_lookup=(variety, version))
            st.rerun()

def _render_chat_pair_actions(ws, current_pair, *, timing_already_shown=False):
    version = ws.session.future_pair.version
    if not timing_already_shown:
        _render_chat_timing_action(version, current_pair["option_a"]["sale_date"], current_pair["options_b"][0]["sale_date"])
    _render_later_shipment_edit(ws, current_pair, version)

def _chat_basis_target(ws, reply):
    from agents.future_pair_state import LABELS

    pair = ws.session.future_pair
    if pending := pair.pending:
        latest_pair = _chat_pair(reply)
        if latest_pair and latest_pair.get("future_pair_version") == pair.version:
            if (pending["status"] == "NEEDS_INPUT" and pair.inputs["price_a"] is not None
                    and LABELS["price_basis"] in pending.get("missing_fields", ())
                    and not pending.get("manual_price_required")):
                return "future_pair", pair.version
            return None
    pending = pair.pending or {}
    if (pending.get("future_pair_version") == pair.version
            and pending.get("status") == "NEEDS_INPUT"
            and LABELS["price_basis"] in pending.get("missing_fields", ())
            and pair.inputs["price_a"] is not None
            and not pending.get("manual_price_required")):
        return "future_pair", pair.version
    if reply.get("state", {}).get("version") == ws.store.version and ws.store.scenario["current_price_per_kg"] is not None:
        for entry in reply.get("visible_results", ()):
            result = entry["result"]
            if (entry["tool"] == "compare_shipment_options" and result["status"] == "NEEDS_INPUT"
                    and "price_basis" in result.get("missing_fields", ())):
                return "comparison", ws.store.version
    return None

def _render_chat_basis(ws, messages):
    reply = messages[-1].get("reply", {})
    target = _chat_basis_target(ws, reply)
    if target is None:
        return
    deferred = any(message.get("reply", {}).get("deferred_basis") == target for message in messages)
    reselection_key = f"basis_reselect_{target[0]}_{target[1]}"
    if deferred and not st.session_state.get(reselection_key):
        st.button("수수료 기준 다시 선택", key=f"{reselection_key}_button",
                  on_click=_open_basis_reselection, args=(reselection_key,))
        return
    subject = "먼저 출하할 때 단가" if target[0] == "future_pair" else "지금 출하할 때 단가"
    answers = (f"{subject}는 수수료를 빼기 전 금액이에요.", f"{subject}는 수수료를 뺀 뒤 금액이에요.",
               "수수료를 뺀 뒤의 단가인지 잘 모르겠어요.")
    choices = list(_BASIS_CHOICES.items())[:2] if deferred else list(_BASIS_CHOICES.items())
    with st.container(horizontal=True, gap="small", key="shipment_basis_choices"):
        for (label, basis), answer in zip(choices, answers):
            st.button(label, key=f"chat_basis_{target[0]}_{target[1]}_{basis}",
                      on_click=_submit_shipment_question, args=(answer,), kwargs={"basis_choice": (*target, basis)})


def _open_basis_reselection(key):
    st.session_state[key] = True


def _reply_to_chat_basis(ws, scope, version, basis, *, on_progress=None):
    pair = ws.session.future_pair
    current_version = pair.version if scope == "future_pair" else ws.store.version
    if version != current_version:
        return {"status": "INVALID_INPUT", "state": ws.store.snapshot(),
                "text": "비교 조건이 바뀌었어요. 현재 조건에서 다시 선택해 주세요."}
    if basis is None:
        text = ("수수료 차감 기준은 아직 미확인으로 둘게요. 임의로 전·후를 정하지 않습니다. "
                "기준을 확인하거나 계산을 위한 가정을 직접 정하면 이어서 비교할 수 있어요. "
                "기준을 모르는 동안에는 이 단가로 총수취액을 계산할 수 없습니다.")
        return {"status": "OK", "state": ws.store.snapshot(), "text": text, "deferred_basis": (scope, version)}
    st.session_state.pop(f"basis_reselect_{scope}_{version}", None)
    if scope == "future_pair":
        return _reply_to_future_pair_edit(ws, version, {"price_basis": basis}, on_progress=on_progress)
    ws.store.origin_turn_id = None
    update = ws.store.update({"price_basis": basis}, version)
    if update["status"] != "OK":
        return {"status": update["status"], "state": ws.store.snapshot(), "text": update["message"]}
    if on_progress:
        on_progress("compare")
    result = ws.calculate(["수수료 차감 전·후 기준"], channel="대화와 공유 조건")
    entries = [{"tool": "compare_shipment_options", "result": result}]
    return {"status": result["status"], "state": ws.store.snapshot(), "visible_results": entries,
            "text": render_dialogue(entries)}

def _render_pending_pair_price(ws, messages):
    from services.shipment_timing import resolve_pair_dates

    state = ws.session.future_pair
    pair = state.pending
    if not pair or pair["future_pair_version"] != state.version:
        return
    if not pair.get("manual_price_required") and (state.specs_deferred or state.costs_deferred):
        earlier, later = resolve_pair_dates(state.inputs)
        _, active_report = _latest_chat_report(messages, ws.store.version, None, state.version)
        if earlier and not (active_report and active_report["kind"] == "shipment_timing"):
            _render_chat_timing_action(state.version, earlier, later)
    blocked_spec = state.specs_deferred and pair.get("source_status") == "NEEDS_CLARIFICATION"
    if not pair.get("manual_price_required") and not blocked_spec:
        return
    key = f"future_pair_chat_manual_{state.version}"
    with st.expander("나중 단가 직접 입력", expanded=st.session_state.get(key) is not None) if blocked_spec else st.container(border=True):
        price = st.number_input("나중 출하의 가정 단가 (원/kg)", min_value=0.01, value=None, key=key)
        if warning := manual_price_warning(state.inputs["price_a"], price):
            st.warning(warning)
        if st.button("이 단가로 비교", key=f"{key}_submit"):
            if price is None:
                st.warning("나중 출하의 가정 단가를 입력해 주세요.")
            else:
                _submit_shipment_question(f"나중 출하 단가를 kg당 {price:,.12g}원으로 비교",
                                          pair_edit=(state.version, {"price_mode": "manual", "price_b": price}))
                st.rerun()

def _render_box_price_actions(pair):
    quote = pair.box_price
    if quote is None or quote.side is None or quote.weight is None or quote.unit_price <= 0:
        return
    st.button("이 단가로 적용", type="primary", key=f"chat_confirm_box_price_{pair.version}",
              on_click=_submit_shipment_question, args=("네, 맞아요.",))
    with st.expander("무게 수정"):
        with st.form(f"chat_box_weight_{pair.version}", border=False):
            weight = st.number_input("상자 하나의 감귤 무게 (kg)", min_value=0.001, max_value=1000.0,
                                     value=float(quote.weight), format="%.3f")
            if st.form_submit_button("이 무게로 다시 환산"):
                _submit_shipment_question(f"상자 한 개에 {weight:g}kg이에요.")
                st.rerun()

def _chat_pair(reply):
    return next((entry["result"] for entry in reply.get("visible_results", [])
                 if entry["tool"] == "compare_future_pair"), None)

def _chat_report(ws, reply):
    timing = next((entry["result"] for entry in reply.get("visible_results", [])
                   if entry.get("tool") == "analyze_shipment_timing" and
                   entry.get("result", {}).get("status") in {"OK", "INSUFFICIENT_DATA"}), None)
    if timing is not None:
        from services.shipment_timing_bundle import report_entries, json_from_entries, source_fingerprint
        economic = ws.session.future_pair.current_result()
        entries = report_entries(timing, economic)
        return {"kind": "shipment_timing", "version": ws.store.version,
                "pair_version": ws.session.future_pair.version,
                "source_sha256": timing["source"]["data_sha256"],
                "source_fingerprint": source_fingerprint(),
                "entries": entries, "json": json_from_entries(entries)}
    pair = _chat_pair(reply)
    if pair is not None:
        state = ws.session.future_pair
        result = state.current_result()
        if pair["status"] != "OK" or result is None or pair["future_pair_version"] != state.version:
            return None
        return {
            "kind": "future_pair", "version": pair["future_pair_version"],
            "entries": [{"tool": "compare_future_pair", "result": result}],
            "json": json.dumps({"format": "jeju-future-pair", "version": 1, "result": result},
                               ensure_ascii=False, indent=2),
        }
    if not any(item["tool"] == "compare_shipment_options" and item["result"]["status"] == "OK"
               for item in reply.get("visible_results", [])):
        return None
    entries = ws.current_entries() or reply["visible_results"]
    try:
        report_json = ws.export_case("내 출하 비교")
    except ValueError:
        report_json = ""
    return {"kind": "comparison", "version": reply.get("state", {}).get("version", ws.store.version),
            "entries": entries, "json": report_json}

def _latest_chat_report(messages, version, current_pair, pair_version=None):
    for index in range(len(messages) - 1, -1, -1):
        reply = messages[index].get("reply", {})
        if report := reply.get("chat_report"):
            if report["kind"] == "future_pair":
                current = current_pair == report["entries"][0]["result"]
            elif report["kind"] == "shipment_timing":
                from services.shipment_timing_bundle import source_fingerprint
                economic = next((entry["result"] for entry in report["entries"]
                                 if entry["tool"] == "compare_future_pair"), None)
                current = (version == report["version"] and pair_version == report["pair_version"]
                           and tuple(report["source_fingerprint"]) == source_fingerprint()
                           and (economic is None or economic == current_pair))
            else:
                current = version == report["version"]
            return (index, report) if current else (None, None)
        if any(item["tool"] in {"compare_shipment_options", "compare_future_pair"} and
               item["result"].get("status") in {"OK", "NEEDS_INPUT", "NEEDS_CLARIFICATION", "NO_DATA"}
               for item in reply.get("visible_results", [])):
            break
    return None, None

def _render_chat_report(report):
    if report["kind"] == "shipment_timing":
        from ui.shipment_timing_ui import render_integrated_chat_report
        render_integrated_chat_report(report)
    elif report["kind"] == "future_pair":
        from ui.future_pair_ui import render_future_pair_result
        render_future_pair_result(report["entries"][0]["result"], "chat", report_json=report["json"])
    else:
        _render_report(report["entries"], chat_json=report["json"])

def _render_shipment_chat(ws, key_present, draft_pending, settlement_dirty, current_pair):
    if not key_present:
        st.title("감귤, 언제 출하할까요?")
        st.info("AI 연결이 아직 설정되지 않았어요. 위의 ‘직접 입력’에서 출하 조건을 비교하거나 ‘자료·도구’에서 과거 가격을 조회할 수 있어요.")
        _render_shipment_composer(disabled=True, show_examples=False)
        return
    if draft_pending or settlement_dirty:
        st.warning("직접 입력에서 수정 중인 내용이 있습니다. 적용하거나 취소한 뒤 대화를 이어가세요.")
    messages = st.session_state.setdefault("agent_messages", [])
    pending = messages[-1] if messages and messages[-1].get("pending") else None
    disabled = draft_pending or settlement_dirty or pending is not None
    if not messages:
        st.title("감귤, 언제 출하할까요?")
        st.write("출하할 수 있는 기간과 궁금한 점을 알려주세요. 가격 흐름을 살펴보고, 기다릴 때의 이득과 보관 부담을 함께 비교해요.")
        st.caption("예: 감귤 4톤을 11월 23일부터 말일까지 출하할 수 있어요. 언제 파는 게 좋을까요?")

    pair_state = ws.session.future_pair
    latest_pair_message = next((index for index in reversed(range(len(messages)))
                                if any(entry["tool"] == "compare_future_pair"
                                       for entry in messages[index].get("reply", {}).get("visible_results", []))), None)
    previous_pair_actions = None
    previous_report_slot = None
    report_index, previous_report = _latest_chat_report(messages, ws.store.version, current_pair, ws.session.future_pair.version)
    transcript = st.container(key="shipment_transcript", height=640, border=False, autoscroll=False) if messages else None
    for index, message in enumerate(messages):
        with transcript.chat_message(message["role"]):
            reply = message.get("reply")
            if not reply:
                st.markdown(message["content"])
                continue
            pair = _chat_pair(reply)
            past_pair = pair is not None and (
                pair.get("future_pair_version") != pair_state.version
                or (pair["status"] == "OK" and current_pair is None)
            )
            if steps := reply.get("progress_steps"):
                with st.status(_shipment_work_label(reply.get("progress_elapsed")),
                               state=_shipment_progress_state(reply.get("status")), type="compact"):
                    for step in steps:
                        st.write(_SHIPMENT_STEP_DETAILS.get(step, step))
            _show_agent_reply(reply, ws.store.version, draft_pending, past_pair)
            if index == report_index and not (draft_pending or settlement_dirty):
                if pending is None:
                    _render_chat_report(previous_report)
                else:
                    previous_report_slot = st.empty()
            if (pair is None or pair["status"] != "OK" or past_pair or index != latest_pair_message
                    or draft_pending or settlement_dirty):
                continue
            if pending is not None:
                previous_pair_actions = (st.empty(), pair)
                continue
            _render_chat_pair_actions(ws, current_pair,
                                      timing_already_shown=bool(previous_report and previous_report["kind"] == "shipment_timing"))

    # The first input belongs beside the welcome copy; later turns stay pinned.
    composer_slot = getattr(st, "bottom", None) if messages else None
    with composer_slot if composer_slot is not None else st.container(key="shipment_welcome_input"):
        with st.container(key="shipment_footer"):
            if messages:
                turn = sum(message["role"] == "user" for message in messages)
                working = pending is not None
                label = "답변 생성 중. 최신 답변으로 이동" if working else "최신 답변으로 이동"
                st.html(f'''<button class="shipment-latest" type="button" data-turn="{turn}"
                    data-running="{str(working).lower()}" aria-label="{label}" title="최신 답변으로 이동"
                    {'' if working else 'hidden'}>
                    <span class="shipment-dots" aria-hidden="true"><i></i><i></i><i></i></span>
                    <span class="shipment-arrow" aria-hidden="true">↓</span>
                </button>''')
            composer = st.empty()
    if pending is not None:
        with composer.container():
            _render_shipment_composer(disabled=True, show_examples=False, busy=True)
        with transcript.chat_message("assistant"):
            pending.pop("pending")
            started = pending.pop("started", None)
            submitted_ms = pending.pop("submitted_ms", None)
            choice = pending.pop("basis_choice", None)
            short_answer = pending["content"].rstrip(".!? ")
            if choice is None and len(messages) > 1 and short_answer in _BASIS_CHOICES:
                if target := _chat_basis_target(ws, messages[-2].get("reply", {})):
                    choice = (*target, _BASIS_CHOICES[short_answer])
            if started is not None:
                log_latency("submit_to_agent", started)
            if lookup := pending.pop("timing_lookup", None):
                reply, steps, elapsed = _run_with_shipment_progress(
                    lambda report: ws.lookup_shipment_timing(*lookup, on_progress=report), started
                )
            elif edit := pending.pop("pair_edit", None):
                reply, steps, elapsed = _run_with_shipment_progress(
                    lambda report: _reply_to_future_pair_edit(ws, *edit, on_progress=report), started
                )
            elif choice is not None:
                reply, steps, elapsed = _run_with_shipment_progress(
                    lambda report: _reply_to_chat_basis(ws, *choice, on_progress=report), started
                )
            elif _REPORT_REQUEST.fullmatch(pending["content"]):
                report = previous_report
                if report is None:
                    economic = pair_state.current_result()
                    if economic is not None:
                        report = _chat_report(ws, {"visible_results": [{"tool": "compare_future_pair",
                                                                          "result": economic}]})
                message = "방금 비교한 결과의 보고서입니다." if report else "내려받을 최근 출하 비교 결과가 없습니다. 출하 조건을 비교한 뒤 다시 요청해 주세요."
                reply = {"status": "OK", "text": message, "state": ws.store.snapshot(), "chat_report": report}
                steps, elapsed = [], None
            else:
                reply, steps, elapsed = _run_with_shipment_progress(
                    lambda report: ws.run(pending["content"], on_progress=report), started
                )
                metrics = reply.get("metrics", {})
                if reply["status"] != "OK" or metrics.get("model_requests"):
                    st.session_state.last_agent_reply = {"status": reply["status"], "metrics": metrics}
            if "chat_report" not in reply:
                reply["chat_report"] = _chat_report(ws, reply)
            ready = monotonic()
            messages.append({"role": "assistant", "reply": {**reply, "progress_steps": steps,
                                                            "progress_elapsed": elapsed}})
            _show_agent_reply(reply, ws.store.version)
            if submitted_ms is not None:
                log_latency("answer_queued", ready)
                log_latency("submit_to_answer_queued", started)
                queued_ms = int(time() * 1000)
                st.html(f"""<span id="jeju-latency-{submitted_ms}" hidden></span><script>
                    requestAnimationFrame(() => requestAnimationFrame(() => {{
                        const marker = document.getElementById('jeju-latency-{submitted_ms}');
                        if (marker) {{
                            marker.dataset.submitToDisplayMs = Date.now() - {submitted_ms};
                            marker.dataset.queueToDisplayMs = Date.now() - {queued_ms};
                        }}
                    }}));
                    </script>""", unsafe_allow_javascript=True)
            if report := reply.get("chat_report"):
                _render_chat_report(report)
            pair = _chat_pair(reply)
            new_current_pair = pair_state.current_result()
            if pair is not None and pair["status"] == "OK" and new_current_pair is not None:
                _render_chat_pair_actions(ws, new_current_pair)
        active_report_index, active_report = _latest_chat_report(
            messages, ws.store.version, new_current_pair, pair_state.version
        )
        if pair is None and previous_pair_actions is not None:
            slot, previous_pair = previous_pair_actions
            if previous_pair["future_pair_version"] == pair_state.version and new_current_pair is not None:
                with slot.container():
                    _render_chat_pair_actions(ws, new_current_pair,
                                              timing_already_shown=bool(active_report and active_report["kind"] == "shipment_timing"))
        if previous_report_slot is not None and active_report_index == report_index:
            with previous_report_slot.container():
                _render_chat_report(previous_report)
    if transcript is not None and not (draft_pending or settlement_dirty):
        with transcript:
            _render_chat_basis(ws, messages)
            _render_pending_pair_price(ws, messages)
            _render_box_price_actions(pair_state)
    with composer.container():
        _render_shipment_composer(draft_pending or settlement_dirty, show_examples=not messages)
