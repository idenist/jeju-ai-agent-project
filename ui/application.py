"""Shared conversational/card workspace. CSV tools and PDF work without a model key."""
from __future__ import annotations

import hashlib
import hmac
import streamlit as st

from core.config import Settings
from presenters.presentation import connection_label


def _render_admin_csv_manager(password: str) -> None:
    with st.expander("관리자 기능 · 가격자료 관리", expanded=False):
        if not password:
            st.caption("가격자료 변경 기능은 잠겨 있습니다. 실행 환경에 JEJU_ADMIN_PASSWORD를 설정하면 사용할 수 있습니다.")
            return
        fingerprint = hashlib.sha256(password.encode("utf-8")).hexdigest()
        auth_key = "market_admin_authorized_fingerprint"
        if st.session_state.get(auth_key) not in (None, fingerprint):
            st.session_state.pop(auth_key, None)
        if st.session_state.get(auth_key) != fingerprint:
            with st.form("market_admin_unlock", clear_on_submit=True):
                candidate = st.text_input("관리자 비밀번호", type="password")
                submitted = st.form_submit_button("관리자 확인")
            if submitted:
                if hmac.compare_digest(candidate.encode("utf-8"), password.encode("utf-8")):
                    st.session_state[auth_key] = fingerprint
                    st.rerun()
                st.error("비밀번호를 확인해 주세요.")
            return
        if st.button("관리자 잠금", key="market_admin_lock"):
            st.session_state.pop(auth_key, None)
            st.rerun()
        _render_csv_manager(embedded=True)




_SHIPMENT_SCROLL_SCRIPT = """
<script>
(() => {
    const app = document.querySelector('.stApp');
    if (!app || app.dataset.shipmentScrollReady) return;
    app.dataset.shipmentScrollReady = 'true';

    let transcript, turn, frame;
    let follow = true;
    let previousTop = 0;
    const nearEnd = () => transcript.scrollHeight - transcript.clientHeight - transcript.scrollTop <= 16;

    function sync() {
        frame = undefined;
        const view = app.querySelector('.st-key-shipment_transcript');
        const footer = app.querySelector('[data-testid="stBottom"] .st-key-shipment_footer');
        const button = footer?.querySelector('.shipment-latest');
        if (!view || !button) {
            transcript = undefined;
            return;
        }
        if (view !== transcript || button.dataset.turn !== turn) {
            transcript = view;
            turn = button.dataset.turn;
            follow = true;
        }
        if (!transcript.clientHeight) return;
        if (follow) {
            if (!nearEnd()) transcript.scrollTop = transcript.scrollHeight;
            // Focusing the composer can also scroll the outer Streamlit page.
            const bottom = footer.closest('[data-testid="stBottom"]');
            const main = bottom?.closest('[data-testid="stMain"]');
            if (main && bottom.clientHeight) {
                const gap = bottom.getBoundingClientRect().top - transcript.getBoundingClientRect().bottom;
                const delta = Math.round(8 - gap);
                if (Math.abs(delta) > 24) main.scrollTop += delta;
            }
        }
        previousTop = transcript.scrollTop;

        const running = !!footer.querySelector('.st-key-shipment_composer_busy');
        button.dataset.running = String(running);
        button.setAttribute('aria-label', running ? '답변 생성 중. 최신 답변으로 이동' : '최신 답변으로 이동');
        button.hidden = !running && nearEnd();
    }

    function schedule() {
        if (frame === undefined) frame = requestAnimationFrame(sync);
    }

    app.addEventListener('scroll', event => {
        if (event.target !== transcript || !transcript.clientHeight) return;
        if (nearEnd()) follow = true;
        else if (transcript.scrollTop < previousTop) follow = false;
        previousTop = transcript.scrollTop;
        schedule();
    }, true);
    app.addEventListener('click', event => {
        if (!transcript || !event.target.closest('.shipment-latest')) return;
        follow = true;
        if (event.detail === 0) {
            transcript.tabIndex = -1;
            transcript.focus({preventScroll: true});
        }
        schedule();
    });
    new MutationObserver(schedule).observe(app, {
        childList: true, subtree: true, attributes: true, attributeFilter: ['data-turn', 'aria-selected'],
    });
    window.addEventListener('resize', schedule);
    schedule();
})();
</script>
"""












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

def main():
    st.set_page_config(page_title="제주 감귤 출하 대안 비교", page_icon="🍊",
                       layout="wide", initial_sidebar_state="collapsed")
    st.html("""
        <style>
        .st-key-shipment_views, :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) {
            max-width: 48rem;
            margin-inline: auto;
        }
        .st-key-shipment_chat_area h1 {
            font-size: clamp(1.75rem, 4vw, 2.75rem);
            word-break: keep-all;
        }
        .st-key-shipment_transcript {
            max-height: max(10rem, calc(100dvh - 20rem));
            overflow-anchor: none;
            overscroll-behavior-y: contain;
        }
        .st-key-shipment_footer {
            position: relative;
        }
        .st-key-shipment_footer [data-testid="stElementContainer"]:has(.shipment-latest) {
            position: absolute;
            top: 0;
            width: 100%;
            z-index: 2;
            pointer-events: none;
        }
        .shipment-latest {
            position: absolute;
            bottom: .25rem;
            left: 50%;
            transform: translateX(-50%);
            display: grid;
            place-items: center;
            min-width: 2.75rem;
            height: 2.75rem;
            padding-inline: .625rem;
            border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
            border-radius: 2rem;
            color: inherit;
            background: var(--st-background-color, Canvas);
            pointer-events: auto;
            cursor: pointer;
        }
        .shipment-latest[hidden], .shipment-dots,
        .shipment-latest[data-running="true"] .shipment-arrow {
            display: none;
        }
        .shipment-latest[data-running="true"] .shipment-dots {
            display: flex;
            align-items: center;
            gap: .25rem;
        }
        .shipment-latest[data-running="true"]:is(:hover, :focus-visible) .shipment-dots {
            display: none;
        }
        .shipment-latest[data-running="true"]:is(:hover, :focus-visible) .shipment-arrow {
            display: block;
        }
        .shipment-arrow {
            font-size: 1.4rem;
            line-height: 1;
        }
        .shipment-latest:focus-visible {
            outline: 2px solid #c85c2c;
            outline-offset: 2px;
        }
        .shipment-dots i {
            width: .3rem;
            height: .3rem;
            border-radius: 50%;
            background: currentColor;
            animation: shipment-dot-wave 1s ease-in-out infinite;
        }
        .shipment-dots i:nth-child(2) { animation-delay: .13s; }
        .shipment-dots i:nth-child(3) { animation-delay: .26s; }
        @keyframes shipment-dot-wave {
            0%, 60%, 100% { transform: translateY(0); opacity: .65; }
            30% { transform: translateY(-.24rem); opacity: 1; }
        }
        @media (prefers-reduced-motion: reduce) {
            .shipment-dots i { animation: none; }
        }
        .stApp:has([data-testid="stBottom"] .st-key-shipment_composer_busy) .st-key-shipment_welcome_input {
            display: none;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) [data-testid="stHorizontalBlock"] {
            display: grid;
            grid-template-columns: minmax(0, 1fr) max-content;
            align-items: end;
            gap: .375rem;
            padding: .25rem .375rem;
            border: 2px solid #9a5833;
            border-radius: 1rem;
        }
        .st-key-shipment_composer [data-testid="stHorizontalBlock"]:focus-within {
            border-color: #c85c2c;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) [data-testid="stColumn"] {
            width: auto;
            min-width: 0;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) :is([data-testid="stTextInputRootElement"], [data-baseweb="input"]) {
            border-color: transparent;
            min-height: 2.75rem;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) [data-testid="stForm"] {
            padding: 0;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) button[kind="primaryFormSubmit"] {
            min-height: 2.75rem;
        }
        .st-key-shipment_composer button[kind="primaryFormSubmit"]:not(:disabled) {
            background: #aa451b;
            border-color: #aa451b;
            color: white;
        }
        .st-key-shipment_composer button[kind="primaryFormSubmit"]:hover:not(:disabled) {
            background: #8c3314;
        }
        .st-key-shipment_examples button {
            min-height: 2.25rem;
            border-radius: 1rem;
            padding-inline: .75rem;
            background: transparent;
        }
        .st-key-shipment_examples button:hover:not(:disabled) {
            border-color: #aa451b;
            background: rgba(170, 69, 27, .12);
            color: inherit;
        }
        .st-key-shipment_examples button:focus-visible {
            outline: 2px solid #c85c2c;
            outline-offset: 2px;
        }
        </style>
    """)
    st.html(_SHIPMENT_SCROLL_SCRIPT, unsafe_allow_javascript=True)
    settings = Settings.load()
    if settings.api_key:
        _preload_model_sdk()
    ws = _workspace()
    if ws.check_data_change():
        st.session_state.agent_messages = []
        st.session_state.pop("pdf_cache", None)
        st.session_state.pop("sensitivity_result", None)
        st.info("사용 자료가 바뀌어 이전 대화·결과를 비웠습니다. 비교 조건은 유지했습니다.")
    if ws.session.settings != settings:
        ws.session.settings = settings
        ws.session.client = None
        st.session_state.pop("last_agent_reply", None)
    _sync_card(force=st.session_state.pop("_force_card_sync", False))
    from ui.settlement_ui import hide_pending_settlement
    settlement_dirty = hide_pending_settlement(ws)
    with st.sidebar:
        st.subheader("설정 · 자료")
        connection_slot = st.empty()
        with st.expander("자료 상태", expanded=False):
            try:
                from services.data_refresh import market_status
                market = market_status()
                st.caption(f"경매가격 자료: {market['observed_first_date']}~{market['observed_last_date']} 관측분 · 현재 실시간 시세는 아닙니다.")
                st.caption("과거 출하량 자료: 2018-01-01~2025-09-17 관측분 · 현재 출하량은 아닙니다.")
            except (OSError, ValueError, KeyError, TypeError):
                st.warning("자료 상태를 읽지 못했습니다.")
        with st.expander("대화와 비교 관리", expanded=False):
            st.button("설정 상태 새로 확인")
            st.button("대화만 비우기 · 조건 유지", on_click=_clear_chat_only)
            st.button("새 비교 시작 · 조건도 초기화", on_click=_reset_all)
        if notice := st.session_state.pop("csv_applied_notice", None):
            st.success(notice)
        _render_admin_csv_manager(settings.admin_password)
        case_download_slot, case_name = _render_cases(ws, _card_dirty())
    with st.container(key="shipment_views"):
        compare_view, tools = st.tabs(
            ["출하 비교", "자료·도구"],
            key="shipment_view", on_change="rerun")
        with compare_view:
            chat, direct = st.tabs(["AI와 대화", "직접 입력"],
                                   key="shipment_input_method", on_change="rerun")
            with chat, st.container(key="shipment_chat_area"):
                chat_slot = st.empty()
                chat_report = st.empty()
            dirty_before = _card_dirty()
            messages = st.session_state.get("agent_messages", [])
            chat_running = bool(settings.api_key and messages and messages[-1].get("pending"))
            if not (compare_view.open and chat.open):
                st.html('<style>[data-testid="stBottom"]:has(:is(.st-key-shipment_composer, .st-key-shipment_composer_busy)) {display: none}</style>')
            if chat_running:
                # Run the turn before the condition widgets bind to the shared state.
                with chat_slot.container():
                    _render_shipment_chat(ws, True, dirty_before, settlement_dirty, ws.session.future_pair.current_result())
                _sync_card()
                dirty_before = _card_dirty()
            future_result = ws.session.future_pair.current_result()
            with direct:
                pair_tab, now_tab, volume_tab = st.tabs(["두 출하일 비교", "지금 판매 vs 보관 후 판매", "과거 출하량만 보기"])
                with pair_tab:
                    from ui.future_pair_ui import render_future_pair
                    future_result = render_future_pair(ws.session.future_pair)
                    from ui.shipment_timing_ui import render_shipment_timing_panel
                    render_shipment_timing_panel(ws.session.future_pair)
                with now_tab:
                    _render_card_summary(ws)
                    if st.button("현재 조건으로 다시 계산", disabled=dirty_before):
                        ws.calculate()
                    dirty = _render_cards(ws)
                    comparison_report = st.empty()
                with volume_tab:
                    from ui.shipment_timing_ui import render_shipment_lookup
                    render_shipment_lookup()
        with tools:
            data_tab, tools_tab = st.tabs(["자료 조회", "추가 도구"])
            with data_tab:
                if st.button("과거 경락가격", disabled=dirty):
                    st.session_state.show_price_history = True
                if st.button("과거 출하자료", disabled=dirty):
                    st.session_state.show_history = True
                from ui.seasonal_prices_ui import render_seasonal_prices
                render_seasonal_prices()
                _render_price_history(ws, dirty)
                _render_history(ws, dirty)
                _render_discovery(ws, dirty)
                data_report = st.empty()
            with tools_tab:
                if st.button("용어 이해", disabled=dirty):
                    from agents.tools import explain_term
                    ws._freeze([{"tool": "explain_term", "result": explain_term("loss_rate")}], "용어 이해")
                _render_sensitivity(ws, dirty)
                _render_offer_limits(ws, dirty)
                _render_review(ws, dirty)
                from ui.settlement_ui import render_shipments
                render_shipments(ws, dirty)
                tools_report = st.empty()
                if not dirty:
                    _render_task_context(ws)
                with st.expander("사용 안내와 자료의 한계"):
                    st.markdown("모르는 값은 빈칸으로 두고, 비용이 없다고 확인한 경우에만 0을 입력하세요. 기간 전체 감모와 하루 비용을 구분하고, 단가에 포함된 비용은 한 번만 반영합니다. 실제 출하 전에 저장시설·계약·납기 조건을 확인하세요.")
                    st.caption("경매 관측가격, 농가 제시가격, 출하량, 시장 반입량은 각각 다른 자료입니다.")
    # Keep the composer mounted so switching tabs also preserves unsubmitted text.
    if not chat_running:
        with chat_slot.container():
            _render_shipment_chat(ws, bool(settings.api_key), dirty, settlement_dirty, future_result)
    connection_slot.write(connection_label(bool(settings.api_key), st.session_state.get("last_agent_reply")))
    if dirty:
        st.warning("조건을 수정했습니다. ‘수정한 조건 저장 후 비교’를 눌러 새 결과를 확인하세요. 이전 결과는 숨겼습니다.")
        st.session_state.pop("sensitivity_result", None)
    if not dirty:
        entries = ws.current_entries()
        tool_names = {entry["tool"] for entry in entries}
        report_slot = comparison_report
        if any(entry["result"].get("ui_context", {}).get("channel") == "대화와 공유 조건" for entry in entries):
            report_slot = chat_report
        elif tool_names & {"compare_sensitivity", "solve_offer_limits", "build_review_brief",
                         "review_case_changes", "explain_decision_conditions", "explain_term"}:
            report_slot = tools_report
        elif not tool_names & {"compare_shipment_options"} and tool_names & {
            "get_price_history", "get_historical_shipment_context", "get_market_evidence", "explore_data"
        }:
            report_slot = data_report
        has_chat_card = any(
            (entry["tool"] == "compare_shipment_options" and entry["result"]["status"] in {"OK", "NEEDS_INPUT"})
            or (entry["tool"] == "analyze_shipment_timing" and entry["result"]["status"] in {"OK", "INSUFFICIENT_DATA"})
            for entry in entries
        )
        if report_slot is not chat_report or not has_chat_card:
            with report_slot.container():
                _render_report(entries)
    # 직접 조회·손익 계산 이후 확정된 결과로 사이드바 JSON 다운로드를 생성합니다.
    with case_download_slot.container():
        _render_case_download(ws, case_name, dirty)

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

from ui.chat import (
    _show_agent_reply,
    _shipment_progress_state,
    _shipment_work_label,
    _run_with_shipment_progress,
    _shipment_timer_html,
    _reply_to_future_pair_edit,
    _render_later_shipment_edit,
    _submit_shipment_question,
    _render_shipment_composer,
    _render_chat_timing_action,
    _render_chat_pair_actions,
    _chat_basis_target,
    _render_chat_basis,
    _reply_to_chat_basis,
    _render_pending_pair_price,
    _render_box_price_actions,
    _chat_pair,
    _chat_report,
    _latest_chat_report,
    _render_chat_report,
    _render_shipment_chat,
)

if __name__ == "__main__":
    main()
