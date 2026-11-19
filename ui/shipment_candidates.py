"""Collect common conditions and export the same candidate results shown on screen."""
import json
from itertools import combinations

import streamlit as st

from presenters.reporting import render_report, shipment_candidate_amounts, shipment_candidate_summary
from ui.pdf_report import render_pdf_report


_BASIS = {"수수료 차감 전": "gross", "수수료 차감 후": "commission_net"}
_STORAGE_MODES = {
    "일할 · 보관일수에 따라 계산": "prorated",
    "정액 · 기간 안이면 같은 금액": "fixed",
}
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


def render_shipment_candidates(draft, *, last_reply_text=None, submit_question=None, timing_available=False):
    if not 2 <= len(draft.eligible) <= 3:
        return
    key = f"shipment_candidates_{draft.form_key}_{draft.revision}"
    edit_key = f"shipment_candidates_{draft.form_key}_editing"
    if draft.result is not None:
        _render_candidate_report(draft.result, key)
        if st.button("후보일 비교 조건 수정", key=f"{key}_edit"):
            had_pending_quote = draft.pending_quote is not None
            draft.invalidate()
            st.session_state[edit_key] = "guided" if had_pending_quote else draft.revision
            st.rerun()
        return
    if (editing := st.session_state.get(edit_key)) is not None:
        can_cancel = editing == draft.revision and not (draft.pending_quote or draft.pending_storage_bill
                                                        or draft.storage_pause or draft.next_condition())
        if st.button("수정 취소" if can_cancel else "차례로 확인", key=f"{key}_guided"):
            st.session_state.pop(edit_key)
            if can_cancel and (result := draft.calculate())["status"] == "OK":
                draft.result = result
            st.rerun()
        _render_candidate_editor(draft, f"{key}_full", edit_key)
        return
    if not draft.has_quote() and not timing_available and submit_question is not None:
        _render_candidate_timing(draft, key, submit_question)
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
        condition_key = f"{key}_{step.field}_{step.day}"
        if step.field == "daily_storage" and draft.pending_storage_bill and submit_question is not None:
            question = step.question if step.question != last_reply_text else None
            _render_storage_bill(draft.pending_storage_bill, question, condition_key, submit_question)
        else:
            _render_condition(draft, step, condition_key)
    elif feedback := draft.condition_feedback():
        st.info(feedback)
        if not draft.has_quote():
            if st.button("견적 다시 입력", key=f"{key}_retry_quote"):
                draft.initial_inputs_saved = False
                draft.invalidate()
                st.rerun()
    if st.button("조건 한 번에 수정", key=f"{key}_details"):
        st.session_state[edit_key] = "guided"
        st.rerun()


def _render_candidate_timing(draft, key, submit):
    if draft.timing_dates == ():
        for days in combinations(draft.eligible, 2):
            label = " · ".join(_date_label(day) for day in days)
            question = "과".join(day.isoformat() for day in days)
            st.button(label, key=f"{key}_timing_{question}", on_click=submit, args=(question,))
    elif draft.timing_dates and not draft.timing_variety:
        from services.shipment_timing import CATEGORIES

        with st.form(f"{key}_timing_variety", border=False):
            variety = st.selectbox("감귤 품종", CATEGORIES, index=None)
            if st.form_submit_button("이 품종으로 조회"):
                if variety is None:
                    st.info("출하할 감귤의 품종을 골라 주세요.")
                else:
                    submit(variety)
                    st.rerun()
    else:
        st.button("이 날짜의 과거 출하량 먼저 보기", key=f"{key}_timing", on_click=submit,
                  args=("이 날짜의 과거 출하량 먼저 보기",))


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


def _render_storage_bill(bill, question, key, submit):
    if question:
        st.markdown(f"**{question}**")
    if bill.days is None:
        with st.form(f"{key}_billing_days", border=False):
            days = st.number_input("청구 기준 보관일", min_value=1, max_value=365, value=None, step=1)
            if st.form_submit_button("이 기간으로 계속"):
                if days is None:
                    st.info("총액이 적용되는 보관일을 알려주세요.")
                else:
                    submit(f"{days}일")
                    st.rerun()
    else:
        st.button("보관일수에 따라 계산", key=f"{key}_prorated", on_click=submit, args=("일할",))
        st.button("기간 안이면 같은 금액", key=f"{key}_fixed", on_click=submit, args=("정액",))
    st.button("잘 모르겠어요", key=f"{key}_cost_unknown", on_click=submit, args=("잘 모르겠어요",))


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
            if step.field == "daily_storage":
                st.caption("총액만 아시면 ‘열흘 보관에 총 10만원’처럼 채팅에 적어 주세요.")
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
        bill = draft.storage_billing
        storage_mode = None
        if bill:
            st.caption(f"저장한 보관비 · {bill['days']}일 기준 총 {bill['total_krw']:,}원")
            option = st.selectbox("보관비 청구 방식", list(_STORAGE_MODES),
                                  index=list(_STORAGE_MODES.values()).index(bill["mode"]))
            storage_mode = _STORAGE_MODES[option]
        daily = _number("공통 하루 보관비(원)", common.get("daily_storage"))
        if bill:
            st.caption("하루 보관비를 입력하면 저장한 기간 보관비를 대체합니다.")
        other = _number("공통 별도 비용(원)", common.get("other_cost"))
        st.caption("비용이 없다고 확인한 항목에는 0을 입력하세요." if bill else
                   "비용이 없다고 확인한 항목에는 0을 입력하세요. 보관일에 따라 총보관비는 달라집니다.")
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
        if bill and daily is not None and storage_mode != bill["mode"]:
            st.warning("청구 방식과 하루 보관비 중 하나만 바꿔 주세요.")
            return
        common = {"start": start, "quantity": quantity, "basis": basis,
                  "daily_storage": daily, "other_cost": other, "fee": fee}
        previous_start = draft.common.get("start")
        st.session_state[edit_key] = "guided"
        if draft.save_form_inputs(common, dates, storage_mode=storage_mode if daily is None else None) or (
            previous_start is not None and previous_start != start and draft.initial_inputs_saved
        ):
            st.session_state.pop(edit_key)
            st.rerun()
        result = draft.calculate()
        if result["status"] == "OK":
            draft.result = result
            st.session_state.pop(edit_key)
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
