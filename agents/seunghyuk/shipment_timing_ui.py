"""Streamlit read-only panel: Hub #855 seasonal supply pattern for A/B shipment dates."""
from __future__ import annotations

import json
from datetime import date

import streamlit as st

from .shipment_timing import CATEGORIES, analyze_shipment_timing, format_timing_report, resolve_pair_dates


def render_shipment_timing_panel(future_pair_inputs: dict | None = None):
    """Called inside the existing '두 미래 출하안 비교' tab; independent of price model/API key."""
    with st.expander("제주데이터허브 출하 시기 분석 · A/B", expanded=False):
        st.caption("제주데이터허브 #855 과거 신고 출하량을 기준으로 두 출하일 주변 7일을 비교합니다. 가격 예측이나 최적 출하일 추천은 아닙니다.")
        variety = st.selectbox("출하통계 품종 · 경매 품종과 별개", list(CATEGORIES),
                               index=None, placeholder="실제 출하 품종을 선택하세요", key="timing_variety")
        planned_a, planned_b = resolve_pair_dates(future_pair_inputs)
        manual = st.checkbox("A/B 출하일 직접 입력", value=False, key="timing_manual_date")
        if not manual and planned_a and planned_b:
            option_a, option_b = planned_a, planned_b
            st.write(f"A안 **{option_a}** · B안 **{option_b}** (상단 A/B 입력 날짜 사용)")
        else:
            if not manual:
                st.caption("A/B 출하일 입력이 아직 없어 날짜를 직접 선택해 주세요.")
            c1, c2 = st.columns(2)
            with c1:
                d1 = st.date_input("A안 출하 예정일", value=None, key="timing_date_a")
            with c2:
                d2 = st.date_input("B안 출하 예정일", value=None, key="timing_date_b")
            option_a = d1.isoformat() if isinstance(d1, date) else None
            option_b = d2.isoformat() if isinstance(d2, date) else None
        args = {"shipment_variety": variety, "option_a_date": option_a, "option_b_date": option_b}
        # No stale result: source file revision and submitted inputs must still match.
        try:
            from .shipment_timing import ROOT, DAILY_PATH, REPORT_PATH
            files = [ROOT / DAILY_PATH, ROOT / REPORT_PATH]
            revisions = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in files)
        except OSError:
            revisions = (None, None)
        signature = (variety, option_a, option_b, revisions)
        if st.button("과거 출하 시기 A/B 분석", key="timing_run", disabled=not variety or not option_a or not option_b):
            st.session_state.timing_result = analyze_shipment_timing(args)
            st.session_state.timing_signature = signature
        result = st.session_state.get("timing_result")
        if result is None:
            return
        if st.session_state.get("timing_signature") != signature:
            st.info("품종·출하일·자료가 변경됐습니다. 다시 분석하면 결과를 표시합니다.")
            return
        if result["status"] == "OK":
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("A 주변 7일 출하량 중앙값", f"{float(result['summary']['a_median_7day_tons']):,.1f}톤")
            with c2:
                st.metric("B 주변 7일 출하량 중앙값", f"{float(result['summary']['b_median_7day_tons']):,.1f}톤")
            with c3:
                st.metric("B 출하량이 더 많았던 연도", f"{result['summary']['b_higher_years']}/{result['paired_year_count']}개")
        elif result["status"] == "INSUFFICIENT_DATA":
            st.warning(result["message"])
        else:
            st.error(result["message"])
        st.markdown(format_timing_report(result))
        st.download_button("분석 결과 JSON", json.dumps(result, ensure_ascii=False, indent=2),
                           file_name="제주데이터허브_출하시기_A_B_분석.json", mime="application/json",
                           key="timing_json_download")
