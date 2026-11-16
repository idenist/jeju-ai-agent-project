"""Direct, no-API-key seasonal same-spec price comparison panel."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import streamlit as st
from services.seasonal_prices import compare_seasonal_prices


def render_seasonal_prices():
    with st.expander("여러 시즌 같은 규격 경락가격 비교", expanded=False):
        st.caption("동일 거래규격의 과거 관측가격만 비교합니다. 미래가격이나 농가 실수취가격은 아닙니다.")
        with st.form("seasonal_prices_form"):
            cols = st.columns(3)
            market = cols[0].text_input("시장코드", value="110001")
            trade = cols[1].selectbox("거래방식", ["경매"])
            variety = cols[2].text_input("품종", value="조생귤")
            cols = st.columns(4)
            grade = cols[0].text_input("등급", value="특")
            size = cols[1].text_input("크기", value="소")
            package = cols[2].text_input("포장", value="상자")
            kg = cols[3].number_input("상자 중량(kg)", min_value=0.001, value=5.0)
            cols = st.columns(2)
            day = cols[0].date_input("비교 월·일", value=date(2024, 11, 15), format="MM/DD/YYYY")
            window = cols[1].selectbox("각 연도 같은 날짜 전후", [0, 3, 7, 14], index=2,
                                        format_func=lambda x: "해당 날짜만" if x == 0 else f"±{x}일")
            submitted = st.form_submit_button("시즌별 가격 비교")
        if submitted:
            st.session_state["seasonal_prices_result"] = compare_seasonal_prices({
                "market_code": market, "trade_type": trade, "variety": variety,
                "grade": grade, "size": size, "package": package, "package_kg": kg,
                "month_day": day.strftime("%m-%d"), "window_days": window,
            })
        result = st.session_state.get("seasonal_prices_result")
        if not result:
            return
        if result["status"] in {"INVALID_INPUT", "NEEDS_INPUT", "AMBIGUOUS_CODES"}:
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
            st.info("아직 가공되지 않은 원본 시즌은 `python -m scripts.prepare_season_prices`로 준비할 수 있습니다.")
