"""Direct, no-API-key seasonal same-spec price comparison panel."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import streamlit as st
from services.seasonal_prices import compare_seasonal_prices, seasonal_price_revision
from ui.market_inputs import render_market_spec_controls


def render_seasonal_prices():
    with st.expander("여러 시즌 같은 규격 경락가격 비교", expanded=False):
        st.caption("동일 거래규격의 과거 관측가격만 비교합니다. 미래가격이나 농가 실수취가격은 아닙니다.")
        selection, complete = render_market_spec_controls("seasonal_prices_spec")
        cols = st.columns(2)
        day = cols[0].date_input("비교할 월·일", value=date.today(), key="seasonal_prices_day",
                                 format="MM/DD/YYYY")
        window = cols[1].selectbox("각 연도에서 볼 범위", [0, 3, 7, 14], index=2,
                                    format_func=lambda x: "해당 날짜만" if x == 0 else f"±{x}일",
                                    key="seasonal_prices_window")
        query = {**selection, "month_day": day.strftime("%m-%d"), "window_days": window}
        sources = seasonal_price_revision()
        previous = st.session_state.get("seasonal_prices_submitted_query")
        if previous is not None and (previous != query or st.session_state.get("seasonal_prices_sources") != sources):
            st.session_state.pop("seasonal_prices_result", None)
            st.session_state.pop("seasonal_prices_submitted_query", None)
            st.info("조회 조건이나 자료가 바뀌어 이전 결과를 숨겼습니다. 다시 조회해 주세요.")
        submitted = st.button("시즌별 가격 비교", disabled=not complete,
                              key="seasonal_prices_submit")
        if submitted:
            st.session_state["seasonal_prices_submitted_query"] = dict(query)
            st.session_state["seasonal_prices_sources"] = sources
            st.session_state["seasonal_prices_result"] = compare_seasonal_prices(query)
        result = st.session_state.get("seasonal_prices_result")
        if not result:
            return
        if result["status"] not in {"OK", "NO_DATA", "PARTIAL_DATA"}:
            st.warning(result["message"])
            return
        if result["status"] == "NO_DATA":
            st.warning("선택한 규격의 가격자료가 없습니다. 아래 시즌별 자료 상태를 확인하세요.")
        rows = []
        labels = {"NOT_PREPARED": "가공본 미생성", "NO_DATA": "해당 규격 거래 없음",
                  "INVALID_CALENDAR_DATE": "해당 연도에 없는 날짜", "AMBIGUOUS_CODES": "분류코드 모호",
                  "DATA_ERROR": "자료 읽기 오류"}
        for item in result["seasons"]:
            rows.append({
                "시즌": item["season"] + (" (부분시즌)" if item["season"] == "2026-2027" else ""),
                "가격(원/kg)": f"{Decimal(item['price_per_kg']):,.2f}" if item["price_per_kg"] is not None else "-",
                "관측일": item["observed_days"], "거래건수": item["transaction_count"],
                "원천기간": f"{item.get('source_first_date') or '-'} ~ {item.get('source_last_date') or '-'}",
                "가격 기간": f"{item.get('window_start', '-') } ~ {item.get('window_end', '-')}",
                "상태": ("관측기간 일부만 확보" if item["status"] == "OK" and not item["window_complete"]
                         else "참고(원천 주의)" if item["quality_warnings"] and item["status"] == "OK"
                         else "조회됨" if item["status"] == "OK" else labels.get(item["status"], item["status"])),
            })
        st.dataframe(rows, hide_index=True, use_container_width=True)
        st.caption("가격은 거래금액 합계 ÷ 거래중량 합계입니다. 품질이 같은 개별 상품을 추적한 값이 아닙니다.")
        if any(item["status"] == "NOT_PREPARED" for item in result["seasons"]):
            st.info("아직 조회할 수 있도록 준비되지 않은 시즌이 있습니다. 자료 담당자에게 해당 시즌의 가격자료 준비를 요청해 주세요.")
