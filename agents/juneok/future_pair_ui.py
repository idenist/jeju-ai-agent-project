"""Direct-entry view for comparing two future shipment dates."""
import json
from datetime import date

import streamlit as st

from agents.common.future_pair import compare_future_pair
from .pdf_report import render_pdf_report
from .reporting import render_report
from .task_context import data_revision


def _cost_fields(side, default_days, gross):
    key = f"future_pair_{side}"
    st.markdown(f"**{side}안 보관 조건**")
    days = st.number_input(f"{side}안 보관일", 1, 365, default_days, key=f"{key}_days")
    loss = st.number_input(f"{side}안 {days}일 전체 감모율(%)", min_value=0.0, max_value=99.99,
                           value=None, key=f"{key}_loss_{days}")
    storage = st.number_input(f"{side}안 하루 보관비(원)", min_value=0.0, value=None, key=f"{key}_storage")
    other = st.number_input(f"{side}안 별도 비용 합계(원)", min_value=0.0, value=None, key=f"{key}_other",
                            help="단가에 포함되지 않은 비용만 입력하세요. 수수료와 보관비는 별도 입력합니다.")
    fee = st.number_input(f"{side}안 수수료율(%)", min_value=0.0, max_value=99.99, value=None,
                          key=f"{key}_fee") if gross else None
    return {"delay_days": days, "loss_percent": loss, "daily_storage_cost": storage,
            "other_cost_krw": other, "fee_percent": fee}


def render_future_pair():
    with st.expander("두 미래 출하안 비교"):
        st.caption("같은 물량을 각각 보관한 뒤 출하할 때의 비교금액입니다. 비용이 없으면 0을 입력하세요.")
        quantity = st.number_input("비교 물량(kg)", min_value=0.0, value=None, key="future_pair_quantity")
        start = st.date_input("두 대안의 보관 시작일", value=date.today(), key="future_pair_start")
        basis = st.selectbox("두 가정 단가의 수수료 기준", ["수수료 차감 전", "수수료 차감 후"],
                             index=None, key="future_pair_basis")
        gross = basis == "수수료 차감 전"
        left, right = st.columns(2)
        with left:
            option_a = _cost_fields("A", 3, gross)
        with right:
            option_b = _cost_fields("B", 10, gross)
        price_a = st.number_input("A안 가정 단가(원/kg)", min_value=0.0, value=None, key="future_pair_price_a")
        mode = st.radio("B안 가정 단가 입력 방식", ["과거 변동으로 가정", "직접 입력"], key="future_pair_price_mode")
        filters = None
        price_b = None
        if mode == "과거 변동으로 가정":
            st.caption(f"과거 비교 간격은 두 출하일의 차이인 {option_b['delay_days'] - option_a['delay_days']}일입니다.")
            filters = {}
            for key, label in [("variety", "품종"), ("grade", "등급"), ("size", "크기"), ("package", "포장 종류"),
                               ("package_kg", "포장 중량(kg)"), ("market_code", "시장코드"), ("trade_type", "거래방식")]:
                value = st.text_input(f"과거가격 {label}", key=f"future_pair_filter_{key}").strip()
                if value:
                    filters[key] = value
        else:
            price_b = st.number_input("B안 가정 단가(원/kg)", min_value=0.0, value=None, key="future_pair_price_b")

        inputs = {"quantity": quantity, "price_basis": ("gross" if gross else "commission_net") if basis else None,
                  "start_date": start.isoformat() if start else None, "option_a": option_a, "option_b": option_b,
                  "price_a": price_a, "price_b": price_b, "history_filters": filters}
        signature = (inputs, data_revision() if filters is not None else None)
        if st.button("미래 A/B 비교 계산", key="future_pair_calculate"):
            st.session_state.future_pair_result = (signature, compare_future_pair(**inputs))
        saved = st.session_state.get("future_pair_result")
        if saved is None:
            return
        if saved[0] != signature:
            st.session_state.pop("future_pair_result", None)
            st.info("비교 조건이나 자료가 바뀌었습니다. 현재 입력으로 다시 계산해 주세요.")
            return
        result = saved[1]
        if result["status"] != "OK":
            st.warning(result.get("message") or result.get("error") or "비교 조건을 확인해 주세요.")
            if result.get("missing_fields"):
                labels = {"variety": "품종", "grade": "등급", "size": "크기", "package": "포장 종류",
                          "package_kg": "포장 중량", "market_code": "시장코드", "trade_type": "거래방식"}
                fields = [labels.get(key, key) for key in result["missing_fields"]]
                st.caption("확인할 항목: " + ", ".join(fields))
            return
        entries = [{"tool": "compare_future_pair", "result": result}]
        st.markdown(render_report(entries))
        try:
            pdf = render_pdf_report(entries)
        except Exception:
            st.warning("미래 A/B PDF를 생성하지 못했습니다. 화면과 JSON으로 결과를 확인할 수 있습니다.")
        else:
            st.download_button("미래 A/B PDF 내려받기", pdf, "미래출하비교.pdf", "application/pdf", on_click="ignore")
        content = json.dumps({"format": "jeju-future-pair", "version": 1, "result": result}, ensure_ascii=False, indent=2)
        st.download_button("미래 A/B 결과 JSON 내려받기", content, "미래출하비교.json", "application/json", on_click="ignore")
