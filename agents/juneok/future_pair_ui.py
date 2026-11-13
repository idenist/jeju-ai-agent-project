"""Direct-entry view for comparing two future shipment dates."""
import json
from datetime import date

import streamlit as st

from .future_pair_state import HISTORY_FIELDS, MAX_RATE_PERCENT, future_pair_feedback
from .pdf_report import render_pdf_report
from .reporting import render_report


def _cost_fields(side, default_days, gross):
    key = f"future_pair_{side}"
    st.markdown(f"**{side}안 보관 조건**")
    st.session_state.setdefault(f"{key}_days", default_days)
    days = st.number_input(f"{side}안 보관일", 1, 365, value=None, key=f"{key}_days")
    period = f"{days}일" if days is not None else "기간"
    loss = st.number_input(f"{side}안 {period} 전체 감모율(%)", min_value=0.0, max_value=MAX_RATE_PERCENT,
                           value=None, key=f"{key}_loss_{days}", disabled=days is None)
    storage = st.number_input(f"{side}안 전체 물량 하루 보관비(원)", min_value=0.0, value=None,
                              key=f"{key}_storage")
    other = st.number_input(f"{side}안 별도 비용 합계(원)", min_value=0.0, value=None, key=f"{key}_other",
                            help="단가에 포함되지 않은 비용만 입력하세요. 수수료와 보관비는 별도 입력합니다.")
    fee = st.number_input(f"{side}안 수수료율(%)", min_value=0.0, max_value=MAX_RATE_PERCENT, value=None,
                          key=f"{key}_fee") if gross else None
    return {"delay_days": days, "loss_percent": loss, "daily_storage_cost": storage,
            "other_cost_krw": other, "fee_percent": fee}


def _sync_inputs(state):
    if st.session_state.get("future_pair_card_version", 0) == state.version:
        return False
    values = state.inputs
    for key in list(st.session_state):
        if key.startswith(("future_pair_A_loss_", "future_pair_B_loss_")):
            del st.session_state[key]
    st.session_state.future_pair_start = date.fromisoformat(values["start_date"]) if values["start_date"] else None
    st.session_state.future_pair_basis = {"gross": "수수료 차감 전", "commission_net": "수수료 차감 후"}.get(values["price_basis"])
    st.session_state.future_pair_price_mode = "과거 변동으로 가정" if values["history_filters"] is not None else "직접 입력"
    for field in ("quantity", "price_a", "price_b"):
        value = values[field]
        st.session_state[f"future_pair_{field}"] = float(value) if value is not None else None
    for field in HISTORY_FIELDS:
        st.session_state[f"future_pair_filter_{field}"] = str((values["history_filters"] or {}).get(field, ""))
    for side in ("A", "B"):
        option = values[f"option_{side.lower()}"]
        days = option["delay_days"]
        prefix = f"future_pair_{side}"
        st.session_state[f"{prefix}_days"] = days
        for field, suffix in (("loss_percent", f"loss_{days}"), ("daily_storage_cost", "storage"),
                              ("other_cost_krw", "other"), ("fee_percent", "fee")):
            value = option[field]
            st.session_state[f"{prefix}_{suffix}"] = float(value) if value is not None else None
    st.session_state.future_pair_card_version = state.version
    return True


def render_future_pair(state):
    synced = _sync_inputs(state)
    with st.expander("두 미래 출하안 비교"):
        st.caption("실제로 선택할 수 있는 두 출하일을 비교하세요. 같은 물량을 기준으로 계산하며, 비용이 없으면 0을 입력하세요.")
        quantity = st.number_input("비교 물량(kg)", min_value=0.0, value=None, key="future_pair_quantity")
        st.session_state.setdefault("future_pair_start", date.today())
        start = st.date_input("두 대안의 보관 시작일", value=None, key="future_pair_start")
        basis = st.selectbox("두 가정 단가의 수수료 기준", ["수수료 차감 전", "수수료 차감 후"],
                             index=None, key="future_pair_basis")
        gross = basis == "수수료 차감 전"
        left, right = st.columns(2)
        with left:
            option_a = _cost_fields("A", 3, gross)
        with right:
            option_b = _cost_fields("B", 10, gross)
        price_a = st.number_input("A안 가정 단가(원/kg)", min_value=0.0, value=None, key="future_pair_price_a")
        st.session_state.setdefault("future_pair_price_mode", "과거 변동으로 가정")
        mode = st.radio("B안 가정 단가 입력 방식", ["과거 변동으로 가정", "직접 입력"], index=None,
                        key="future_pair_price_mode")
        filters = None
        price_b = None
        if mode == "과거 변동으로 가정":
            a_days, b_days = option_a["delay_days"], option_b["delay_days"]
            if a_days is not None and b_days is not None and b_days > a_days:
                st.caption(f"두 출하일의 차이인 {b_days - a_days}일 간격의 과거 가격을 찾습니다.")
            filters = {}
            for key, label in HISTORY_FIELDS.items():
                value = st.text_input(f"과거가격 {label}", key=f"future_pair_filter_{key}").strip()
                if value:
                    filters[key] = value
        else:
            price_b = st.number_input("B안 가정 단가(원/kg)", min_value=0.0, value=None, key="future_pair_price_b")

        inputs = {"quantity": quantity, "price_basis": ("gross" if gross else "commission_net") if basis else None,
                  "start_date": start.isoformat() if start else None, "option_a": option_a, "option_b": option_b,
                  "price_a": price_a, "price_b": price_b, "history_filters": filters}
        previous = st.session_state.get("future_pair_card_inputs")
        current = state.current_result()
        if not synced and previous is not None and inputs != previous:
            state.replace(inputs)
            st.session_state.future_pair_card_version = state.version
            if current is not None:
                st.info("비교 조건이나 자료가 바뀌었습니다. 현재 입력으로 다시 계산해 주세요.")
        st.session_state.future_pair_card_inputs = inputs
        if st.button("미래 A/B 비교 계산", key="future_pair_calculate"):
            state.replace(inputs)
            st.session_state.future_pair_card_version = state.version
            result = state.calculate()
        else:
            result = state.current_result()
        if result is None:
            return
        if result["status"] != "OK":
            st.warning(future_pair_feedback(result))
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
