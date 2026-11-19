"""Collect common conditions and export the same candidate results shown on screen."""
import json

import streamlit as st

from presenters.reporting import render_report, shipment_candidate_amounts, shipment_candidate_summary
from ui.pdf_report import render_pdf_report


_BASIS = {"수수료 차감 전": "gross", "수수료 차감 후": "commission_net"}
_GUIDED_NUMBERS = {
    "daily_storage": ("공통 하루 보관비(원)", None),
    "other_cost": ("공통 별도 비용(원)", None),
    "fee": ("공통 판매 수수료율(%) · 차감 전 단가일 때", 99.99),
}


def _date_label(day):
    return f"{day.month}월 {day.day}일"


def _number(label, value=None, *, minimum=0., maximum=None, disabled=False):
    return st.number_input(label, min_value=minimum, max_value=maximum,
                           value=float(value) if value is not None else None, disabled=disabled)


def render_shipment_candidates(draft, *, last_reply_text=None):
    if not 2 <= len(draft.eligible) <= 3:
        return
    key = f"shipment_candidates_{draft.form_key}_{draft.revision}"
    edit_key = f"shipment_candidates_{draft.form_key}_editing"
    if draft.result is not None:
        _render_candidate_report(draft.result, key)
        if st.button("후보일 비교 조건 수정", key=f"{key}_edit"):
            st.session_state[edit_key] = True
            draft.invalidate()
            st.rerun()
        return
    if st.session_state.get(edit_key):
        if st.button("차례로 확인", key=f"{key}_guided"):
            st.session_state[edit_key] = False
            st.rerun()
        _render_candidate_editor(draft, f"{key}_full", edit_key)
        return
    if not draft.initial_inputs_saved:
        _render_initial_inputs(draft, f"{key}_initial")
        return

    if draft.storage_pause:
        feedback = draft.condition_feedback()
        if feedback != last_reply_text:
            st.info(feedback)
        if draft.storage_pause == "waiting" and st.button("나중에 계속", key=f"{key}_later"):
            draft.postpone_storage()
            st.rerun()
        return
    if step := draft.next_condition():
        _render_condition(draft, step, f"{key}_{step.field}_{step.day}")
    elif feedback := draft.condition_feedback():
        st.info(feedback)
        if not any(draft.by_date.get(day, (None, None))[0] is not None for day in draft.eligible):
            if st.button("견적 다시 입력", key=f"{key}_retry_quote"):
                draft.initial_inputs_saved = False
                draft.invalidate()
                st.rerun()
    if st.button("조건 한 번에 수정", key=f"{key}_details"):
        st.session_state[edit_key] = True
        st.rerun()


def _render_initial_inputs(draft, key):
    with st.form(key):
        st.markdown("**먼저 출하일과 견적을 확인해요**")
        st.caption("출하 후보일 · " + ", ".join(_date_label(day) for day in draft.eligible))
        quantity = _number("전체 비교 물량(kg)", draft.common.get("quantity"), minimum=.01)
        if basis := draft.common.get("basis"):
            label = next(name for name, value in _BASIS.items() if value == basis)
            st.caption(f"{label} 기준의 단가를 입력해 주세요.")
        st.caption("견적을 모르는 날짜는 비워두세요.")
        quotes = {day: _number(f"{_date_label(day)} 단가(원/kg)", draft.by_date.get(day, (None, None))[0], minimum=.01)
                  for day in draft.eligible}
        start = st.form_submit_button("후보일 비교 시작", type="primary")
    if start:
        if quantity is None:
            st.warning("비교할 전체 물량을 입력해 주세요.")
            return
        result = draft.save_initial_inputs(quantity, quotes)
        if result["status"] == "INVALID_INPUT":
            st.warning(result["message"])
        else:
            st.rerun()


def _render_condition(draft, step, key):
    with st.form(key):
        st.markdown(f"**{step.question}**")
        if step.field == "basis":
            value = _BASIS.get(st.selectbox("단가의 수수료 기준", list(_BASIS), index=None))
        elif step.field == "start":
            value = st.date_input("보관 시작일", value=None, max_value=draft.eligible[0], format="YYYY-MM-DD")
        elif step.field == "loss":
            value = _number(f"{_date_label(step.day)} 기간 전체 감모율(%)", maximum=99.99)
        else:
            label, maximum = _GUIDED_NUMBERS[step.field]
            value = _number(label, maximum=maximum)
        save = st.form_submit_button("확인하고 계속", type="primary")
        unknown = st.form_submit_button("모르겠어요")
    if save or unknown:
        if save and value is None:
            st.warning("값을 입력하거나 ‘모르겠어요’를 선택해 주세요.")
            return
        result = draft.apply_condition(step, None if unknown else value)
        if result["status"] == "INVALID_INPUT":
            st.warning(result["message"])
        else:
            st.rerun()


def _render_candidate_editor(draft, key, edit_key):
    with st.form(key):
        st.markdown("**후보일 비교 조건**")
        common = draft.common
        start = st.date_input("보관 시작일", value=common.get("start"), max_value=draft.eligible[0])
        quantity = _number("전체 비교 물량(kg)", common.get("quantity"), minimum=.01)
        names = list(_BASIS)
        basis_index = next((index for index, value in enumerate(_BASIS.values()) if value == common.get("basis")), None)
        basis = _BASIS.get(st.selectbox("단가의 수수료 기준", names, index=basis_index))
        saved_net = common.get("basis") == "commission_net"
        saved_fee = common.get("fee")
        if saved_fee is None:
            saved_fee = draft.gross_fee
        fee = _number("공통 판매 수수료율(%) · 차감 전 단가일 때", None if saved_net else saved_fee,
                      maximum=99.99, disabled=saved_net)
        if saved_net:
            st.caption("수수료 차감 후 단가에는 판매 수수료율을 적용하지 않아요.")
        daily = _number("공통 하루 보관비(원)", common.get("daily_storage"))
        other = _number("공통 별도 비용(원)", common.get("other_cost"))
        st.caption("비용이 없다고 확인한 항목에는 0을 입력하세요. 보관일에 따라 총보관비는 달라집니다.")
        st.caption("견적을 아는 후보일의 단가를 하나 이상 입력하세요. 견적이 없는 날짜는 단가를 비워두면 필요 단가를 보여드려요.")
        if any(price is not None for price, _ in draft.by_date.values()):
            st.caption("수수료 기준을 바꿔 제출하면 단가만 새 기준으로 다시 확인해요.")
        elif basis_index is not None:
            st.caption(f"{names[basis_index]} 기준으로 받은 단가를 입력해 주세요.")
        dates = {}
        for day in draft.eligible:
            label = _date_label(day)
            price, loss = draft.by_date.get(day, (None, None))
            quote = _number(f"{label} 단가(원/kg)", price, minimum=.01)
            if price is not None and st.checkbox(f"{label} 기존 견적 지우기"):
                quote = None
            dates[day] = (quote,
                          _number(f"{label} 기간 전체 감모율(%)", loss, maximum=99.99))
        submitted = st.form_submit_button("후보일 비교 계산", type="primary")
    if submitted:
        common = {"start": start, "quantity": quantity, "basis": basis,
                  "daily_storage": daily, "other_cost": other, "fee": fee}
        previous_start = draft.common.get("start")
        if draft.save_form_inputs(common, dates) or (
            previous_start is not None and previous_start != start and draft.initial_inputs_saved
        ):
            st.session_state[edit_key] = False
            st.rerun()
        result = draft.calculate()
        if result["status"] == "OK":
            draft.result = result
            st.session_state[edit_key] = False
            st.rerun()
        st.warning(result["message"])


def _render_candidate_report(result, key):
    entries = [{"tool": "compare_shipment_candidates", "result": result}]
    with st.container(border=True):
        st.markdown("**출하 후보일 비교 결과**")
        st.markdown(shipment_candidate_summary(result))
        st.markdown(shipment_candidate_amounts(result))
        try:
            pdf = render_pdf_report(entries)
        except (ValueError, OSError):
            st.warning("PDF를 만들지 못했습니다. 화면과 JSON에서 결과를 확인할 수 있습니다.")
        else:
            st.download_button("후보일 비교 PDF 저장", pdf, "출하후보일비교.pdf", "application/pdf",
                               key=f"{key}_pdf", on_click="ignore")
        with st.expander("후보일 보고서 미리보기"):
            st.markdown(render_report(entries))
        with st.expander("상세 · 후보일 JSON"):
            artifact = {"format": "jeju-shipment-candidates", "version": 1, "result": result}
            st.download_button("후보일 비교 JSON 저장", json.dumps(artifact, ensure_ascii=False, indent=2),
                               "출하후보일비교.json", "application/json", key=f"{key}_json", on_click="ignore")
