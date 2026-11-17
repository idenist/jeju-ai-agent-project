"""Date-synchronised Hub #855 A/B panel and unified on-screen/PDF/JSON report.

The date is always read from the existing future A/B input; there is no second date form.
No API key, model prediction, or implicit category mapping is required.
"""
from __future__ import annotations

import streamlit as st

from services.shipment_timing import CATEGORIES, analyze_shipment_timing, format_timing_report, resolve_pair_dates
from services.shipment_timing_bundle import (
    aligned_economic, export_json, report_entries, source_fingerprint,
)


def _state_inputs_and_economic(pair_state_or_inputs):
    if isinstance(pair_state_or_inputs, dict):
        return pair_state_or_inputs, None
    if pair_state_or_inputs is None:
        return None, None
    inputs = getattr(pair_state_or_inputs, "inputs", None)
    getter = getattr(pair_state_or_inputs, "current_result", None)
    economic = getter() if callable(getter) else None
    return inputs, economic


def render_integrated_downloads(entries: list[dict], report_json: str, context: str) -> None:
    """One PDF/JSON pair based on the same entries; used by direct UI and chat."""
    from presenters.reporting import render_report

    if any(entry["tool"] == "compare_future_pair" for entry in entries):
        label, filename = "출하 시기·경제성 통합", "제주감귤_출하시기_경제성_통합"
    else:
        label, filename = "과거 출하량", "제주감귤_과거출하량"
    try:
        from ui.pdf_report import render_pdf_report
        pdf = render_pdf_report(entries)
    except Exception:
        st.warning("PDF 생성에 실패했습니다. JSON과 화면 결과는 확인할 수 있습니다.")
    else:
        st.download_button(f"{label} PDF 저장", pdf, f"{filename}.pdf", "application/pdf",
                           key=f"timing_unified_pdf_{context}", on_click="ignore")
    with st.expander("보고서 미리보기 · 연도별 출하량", expanded=False):
        st.markdown(render_report(entries))
    with st.expander("상세 · JSON"):
        st.download_button(f"{label} JSON 저장", report_json, f"{filename}.json", "application/json",
                           key=f"timing_unified_json_{context}", on_click="ignore")


def render_integrated_chat_report(report: dict) -> None:
    """Chat export uses the same canonical entries as the direct-entry panel."""
    with st.container(border=True):
        if len(report["entries"]) == 1:
            st.markdown("**제주 감귤 과거 출하량 보고서**")
        else:
            st.markdown("**제주 감귤 출하 시기·경제성 통합 보고서**")
        render_integrated_downloads(report["entries"], report["json"], "chat")


def render_shipment_timing_panel(pair_state_or_inputs=None):
    """Called under the existing two-future-shipment tab, after the economic result UI."""
    inputs, economic_candidate = _state_inputs_and_economic(pair_state_or_inputs)
    with st.expander("제주데이터허브 출하 시기 분석 · A/B", expanded=False):
        st.caption("위쪽 A/B 보관 시작일·보관일을 그대로 사용합니다. 과거 같은 시기 신고 출하량 분석이며 미래 가격·최적 출하일 예측이 아닙니다.")
        planned_a, planned_b = resolve_pair_dates(inputs)
        if not (planned_a and planned_b):
            st.info("위쪽에서 보관 시작일과 A/B 보관일을 입력해 주세요. 날짜를 다시 입력할 필요는 없습니다.")
            return None
        return _render_shipment_lookup(planned_a, planned_b, economic_candidate)


def render_shipment_lookup():
    st.caption("품종과 두 날짜를 입력하면 과거 같은 시기의 신고 출하량을 비교합니다.")
    earlier = st.date_input("먼저 출하할 날짜", value=None, key="timing_lookup_earlier")
    later = st.date_input("나중 출하할 날짜", value=None, key="timing_lookup_later")
    return _render_shipment_lookup(earlier.isoformat() if earlier else None,
                                   later.isoformat() if later else None, prefix="timing_lookup")


def _render_shipment_lookup(planned_a, planned_b, economic_candidate=None, *, prefix="timing"):
    if planned_a and planned_b:
        st.write(f"분석할 출하일: A **{planned_a}** · B **{planned_b}**")
    variety = st.selectbox("출하통계 품종 · 경매 품종과 별개", list(CATEGORIES),
                           index=None, placeholder="제주데이터허브 품종을 선택하세요", key=f"{prefix}_variety")
    signature = (variety, planned_a, planned_b, source_fingerprint())
    result_key, signature_key = f"{prefix}_result", f"{prefix}_signature"
    if st.button("A/B 과거 출하량 분석", key=f"{prefix}_run", disabled=not (variety and planned_a and planned_b)):
        args = {"shipment_variety": variety, "option_a_date": planned_a, "option_b_date": planned_b}
        st.session_state[result_key] = analyze_shipment_timing(args)
        st.session_state[signature_key] = signature
    result = st.session_state.get(result_key)
    if result is None:
        return None
    if st.session_state.get(signature_key) != signature:
        st.info("품종·날짜·출하량 원본이 변경됐습니다. 다시 분석해 주세요.")
        return None
    if (result["status"] in {"OK", "INSUFFICIENT_DATA"}
            and result.get("source", {}).get("data_sha256") != signature[3][0]):
        st.info("조회 중 자료가 바뀌었습니다. 다시 분석해 주세요.")
        return None
    if result["status"] == "OK":
        summary = result["summary"]
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("A 주변 7일 중앙값", f"{float(summary['a_median_7day_tons']):,.1f}톤")
        with c2:
            st.metric("B 주변 7일 중앙값", f"{float(summary['b_median_7day_tons']):,.1f}톤")
        with c3:
            st.metric("B가 더 많았던 연도", f"{summary['b_higher_years']}/{result['paired_year_count']}개")
    elif result["status"] == "INSUFFICIENT_DATA":
        st.warning(result["message"])
    else:
        st.error(result["message"])
        return result
    st.markdown(format_timing_report(result))
    economic = aligned_economic(result, economic_candidate)
    entries = report_entries(result, economic)
    if economic is None and prefix == "timing":
        st.caption("위에서 경제성 비교를 계산하면 같은 날짜의 경제성 결과를 함께 저장할 수 있습니다.")
    elif economic is not None:
        from presenters.reporting import future_pair_summary
        conclusion, minimum, favorable = future_pair_summary(economic)
        st.markdown("\n\n".join(part for part in
                                  (f"**현재 A/B 경제성 계산과 함께 보기** · {conclusion}", minimum, favorable)
                                  if part))
        st.caption("경제성은 입력한 가정 단가·감모·비용에 따른 계산이며 출하량으로 추정한 미래수익이 아닙니다.")
    context = "direct" if prefix == "timing" else "lookup"
    render_integrated_downloads(entries, export_json(result, economic), context)
    return result
