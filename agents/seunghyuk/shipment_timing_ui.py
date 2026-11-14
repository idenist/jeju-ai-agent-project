"""Date-synchronised Hub #855 A/B panel and unified on-screen/PDF/JSON report.

The date is always read from the existing future A/B input; there is no second date form.
No API key, model prediction, or implicit category mapping is required.
"""
from __future__ import annotations

import json

import streamlit as st

from .shipment_timing import CATEGORIES, analyze_shipment_timing, format_timing_report, resolve_pair_dates
from .shipment_timing_bundle import (
    aligned_economic, export_json, report_entries, source_fingerprint, shipment_source_is_current,
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
    from agents.juneok.reporting import render_report

    with st.expander("통합 보고서 미리보기", expanded=False):
        st.markdown(render_report(entries))
    try:
        from agents.juneok.pdf_report import render_pdf_report
        pdf = render_pdf_report(entries)
    except Exception:
        st.warning("통합 PDF 생성에 실패했습니다. JSON과 화면 결과는 확인할 수 있습니다.")
    else:
        st.download_button("출하 시기·경제성 통합 PDF", pdf,
                           "제주감귤_출하시기_경제성_통합.pdf", "application/pdf",
                           key=f"timing_unified_pdf_{context}", on_click="ignore")
    st.download_button("출하 시기·경제성 통합 JSON", report_json,
                       "제주감귤_출하시기_경제성_통합.json", "application/json",
                       key=f"timing_unified_json_{context}", on_click="ignore")


def render_integrated_chat_report(report: dict) -> None:
    """Chat export uses the same canonical entries as the direct-entry panel."""
    with st.container(border=True):
        st.markdown("**제주데이터허브 출하 시기·경제성 통합 보고서**")
        if len(report["entries"]) == 1:
            st.caption("경제성 입력·계산이 없거나 출하일이 달라 출하량 분석만 포함했습니다.")
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
        st.write(f"분석할 출하일: A **{planned_a}** · B **{planned_b}**")
        variety = st.selectbox("출하통계 품종 · 경매 품종과 별개", list(CATEGORIES),
                               index=None, placeholder="제주데이터허브 품종을 선택하세요", key="timing_variety")
        signature = (variety, planned_a, planned_b, source_fingerprint())
        can_analyze = bool(variety and planned_a and planned_b)
        if st.button("A/B 과거 출하량 분석", key="timing_run", disabled=not can_analyze):
            args = {"shipment_variety": variety, "option_a_date": planned_a, "option_b_date": planned_b}
            st.session_state.timing_result = analyze_shipment_timing(args)
            st.session_state.timing_signature = signature
        result = st.session_state.get("timing_result")
        if result is None:
            return None
        if (st.session_state.get("timing_signature") != signature or
                result.get("source", {}).get("data_sha256") != signature[3][0]):
            st.info("품종·날짜·출하량 원본이 변경됐습니다. 다시 분석해 주세요. 이전 보고서는 표시하지 않습니다.")
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
        if economic is None:
            st.caption("현재 출하량 분석은 완료됐습니다. 위쪽 경제성 비교 조건까지 입력·계산하면 같은 날짜의 경제성 결과도 함께 저장됩니다.")
        else:
            from agents.juneok.reporting import future_pair_summary
            conclusion, minimum = future_pair_summary(economic)
            st.markdown(f"**현재 A/B 경제성 계산과 함께 보기** · {conclusion}\n\n{minimum}")
            st.caption("경제성은 입력한 가정 단가·감모·비용에 따른 계산이며 출하량으로 추정한 미래수익이 아닙니다.")
        render_integrated_downloads(entries, export_json(result, economic), "direct")
        return result
