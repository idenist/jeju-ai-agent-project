# Shared Streamlit session actions; no imports from application/chat/panels.
"""Shared conversational/card workspace. CSV tools and PDF work without a model key."""
from __future__ import annotations

from threading import Thread
import streamlit as st

from agents.agent import AgentSession
from core.config import Settings
from ui.ui_workflow import form_snapshot
from ui.workspace import SharedWorkspace, card_values





def _workspace():
    if "workspace" not in st.session_state:
        session = st.session_state.get("agent_session") or AgentSession(settings=Settings.load())
        st.session_state.workspace = SharedWorkspace(session)
        st.session_state.agent_session = session
    return st.session_state.workspace

@st.cache_resource(show_spinner=False)
def _preload_model_sdk():
    def load():
        from agents.agent import load_model_sdk

        try:
            load_model_sdk()
        except ImportError:
            # The normal request path handles a missing or broken SDK.
            return

    worker = Thread(target=load, name="openai-sdk-preload", daemon=True)
    worker.start()
    return worker

def _card_dirty():
    saved = st.session_state.get("card_form_snapshot")
    return saved is not None and form_snapshot(st.session_state) != saved

def _sync_card(force=False):
    ws = _workspace()
    if force or "card_base_version" not in st.session_state or (st.session_state.card_base_version != ws.store.version and not _card_dirty()):
        values, ids = card_values(ws.store.snapshot())
        st.session_state.update(values)
        st.session_state.input_context = ws.input_context
        st.session_state.card_option_ids = ids
        st.session_state.card_base_version = ws.store.version
        st.session_state.card_form_snapshot = form_snapshot(st.session_state)
        st.session_state.pop("_period_change_notice", None)

def _load_demo():
    _workspace().load_demo()
    _sync_card(force=True)

def _reload_cards():
    _sync_card(force=True)

def _on_period_edit(index):
    st.session_state[f"period_{index}"] = False
    st.session_state._period_change_notice = index + 1

def _clear_chat_only():
    _workspace().clear_conversation(preserve_conditions=True)
    st.session_state.agent_messages = []
    st.session_state.pop("last_agent_reply", None)
    st.session_state.pop("sensitivity_result", None)

def _reset_all():
    _workspace().clear_conversation(preserve_conditions=False)
    st.session_state.agent_messages = []
    st.session_state.pop("last_agent_reply", None)
    st.session_state.pop("sensitivity_result", None)
    for key in ['pdf_cache', 'settlement_pdf', 'settlement_transfer']:
        st.session_state.pop(key, None)
    st.session_state.pop("future_pair_shared_costs", None)
    st.session_state.pop("future_pair_applied_shared_costs", None)
    st.session_state.pop("future_pair_separate_costs", None)
    st.session_state.pop("future_pair_price_drafts", None)
    for key in ("future_pair_missing", "future_pair_history_open", "future_pair_costs_open"):
        st.session_state.pop(key, None)
    for key in list(st.session_state):
        if key == "future_pair_price_b" or key.startswith("future_pair_filter_"):
            del st.session_state[key]
    # 새 비교는 직접 입력한 과거 경락가격의 화면 조건도 초기화합니다.
    for key in ("price_history_variety", "price_history_grade", "price_history_size",
                "price_history_package", "price_history_package_kg", "price_history_market_code",
                "price_history_trade_type", "price_history_interval_days", "price_history_base_date",
                "price_history_base_date_picker", "price_history_limit", "price_history_submitted_query",
                "show_price_history", "seasonal_prices_submitted_query", "seasonal_prices_result",
                "review_as_of_date", "seasonal_prices_sources"):
        st.session_state.pop(key, None)
    for key in list(st.session_state):
        if key.startswith(("price_history_spec_", "seasonal_prices_spec_", "review_market_", "market_decision_")):
            st.session_state.pop(key, None)
    _sync_card(force=True)

def _clear_sensitivity():
    st.session_state.pop("sensitivity_result", None)
    ws = _workspace()
    ws.entries = [] if any(x["tool"] in {"build_review_brief", "explain_decision_conditions"} for x in ws.entries) else [x for x in ws.entries if x["tool"] != "compare_sensitivity"]

def _clear_review():
    _workspace().entries = []
    st.session_state.pop("sensitivity_result", None)
