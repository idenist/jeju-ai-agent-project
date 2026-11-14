"""Direct-entry view for comparing two future shipment dates."""
import json
from datetime import date

import streamlit as st

from .future_pair_state import HISTORY_FIELDS, MAX_RATE_PERCENT, OPTION_LABELS, future_pair_feedback
from .pdf_report import render_pdf_report
from .reporting import render_report


def _storage_period(side, default_days):
    key = f"future_pair_{side}"
    st.session_state.setdefault(f"{key}_days", default_days)
    return st.number_input(f"{side}안 보관일", 1, 365, value=None, key=f"{key}_days")


def _cost_fields(side, days, gross, shared=None):
    key = f"future_pair_{side}"
    period = f"{days}일" if days is not None else "기간"
    loss = st.number_input(f"{side}안 {period} 전체 감모율(%)", min_value=0.0, max_value=MAX_RATE_PERCENT,
                           value=None, key=f"{key}_loss_{days}", disabled=days is None)
    st.caption("비슷한 기간 보관한 기록에서 줄어든 판매 가능 무게를 확인하세요. 100kg 중 4kg이 줄었으면 4를 입력하세요.")
    if shared is not None:
        return {**shared, "delay_days": days, "loss_percent": loss}
    storage = st.number_input(f"{side}안 전체 물량 하루 보관비(원)", min_value=0.0, value=None,
                              key=f"{key}_storage")
    st.caption("저장고 요금표나 견적에서 비교 물량 전체의 하루 요금을 확인하세요. 하루 1만 원이면 10000을 입력하세요.")
    other = st.number_input(f"{side}안 별도 비용 합계(원)", min_value=0.0, value=None, key=f"{key}_other",
                            help="단가에 포함되지 않은 비용만 입력하세요. 수수료와 보관비는 별도 입력합니다.")
    fee = None
    if gross:
        fee = st.number_input(f"{side}안 수수료율(%)", min_value=0.0, max_value=MAX_RATE_PERCENT, value=None,
                              key=f"{key}_fee")
        st.caption("출하처 정산서나 약정에서 판매 수수료율을 확인하세요. 5%면 5를 입력하세요.")
    return {"delay_days": days, "loss_percent": loss, "daily_storage_cost": storage,
            "other_cost_krw": other, "fee_percent": fee}


def _sync_inputs(state):
    if st.session_state.get("future_pair_card_version", 0) == state.version:
        return False
    values = state.inputs
    st.session_state.pop("future_pair_separate_costs", None)
    if any(values["option_a"][field] != values["option_b"][field]
           for field in ("daily_storage_cost", "other_cost_krw", "fee_percent")):
        st.session_state.future_pair_shared_costs = False
    for key in list(st.session_state):
        if key.startswith(("future_pair_A_loss_", "future_pair_B_loss_")):
            del st.session_state[key]
    st.session_state.future_pair_start = date.fromisoformat(values["start_date"]) if values["start_date"] else None
    st.session_state.future_pair_basis = {"gross": "수수료 차감 전", "commission_net": "수수료 차감 후"}.get(values["price_basis"])
    st.session_state.future_pair_price_mode = "과거 변동으로 가정" if values["history_filters"] is not None else "직접 입력"
    for field in ("quantity", "price_a"):
        value = values[field]
        st.session_state[f"future_pair_{field}"] = float(value) if value is not None else None
    drafts = st.session_state.setdefault("future_pair_price_drafts", {})
    if values["history_filters"] is None:
        value = values["price_b"]
        drafts["future_pair_price_b"] = float(value) if value is not None else None
        st.session_state.future_pair_price_b = drafts["future_pair_price_b"]
    else:
        for field in HISTORY_FIELDS:
            key = f"future_pair_filter_{field}"
            drafts[key] = str(values["history_filters"].get(field, ""))
            st.session_state[key] = drafts[key]
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


def _toggle_shared_costs(state):
    fields = {"daily_storage_cost": "storage", "other_cost_krw": "other", "fee_percent": "fee"}
    if st.session_state.future_pair_shared_costs:
        st.session_state.future_pair_separate_costs = {
            field: st.session_state.get(f"future_pair_B_{suffix}", state.inputs["option_b"][field])
            for field, suffix in fields.items()
        }
        return
    costs = st.session_state.pop("future_pair_separate_costs", state.inputs["option_b"])
    for field, suffix in fields.items():
        value = costs[field]
        st.session_state[f"future_pair_B_{suffix}"] = float(value) if value is not None else None


def _open_input_section(section):
    st.session_state[f"future_pair_{section}_open"] = True
    st.session_state.future_pair_input_jump = section


def _show_missing_inputs(result):
    fields = set(result.get("missing_fields", ()))
    cost_fields = {f"{side}안 {label}" for side in ("A", "B")
                   for field, label in OPTION_LABELS.items() if field != "delay_days"}
    if fields - cost_fields - HISTORY_FIELDS.keys():
        st.markdown("[기본 입력으로 이동](#future-pair-basic)")
    if fields & HISTORY_FIELDS.keys():
        st.button("거래규격 입력 열기", key="future_pair_open_history",
                  on_click=_open_input_section, args=("history",))
    if fields & cost_fields:
        st.button("감모율과 비용 입력 열기", key="future_pair_open_costs",
                  on_click=_open_input_section, args=("costs",))


def render_future_pair(state):
    synced = _sync_inputs(state)
    with st.container():
        st.caption("같은 물량을 두 출하일에 판매할 때의 금액을 비교합니다.")
        st.html('<span id="future-pair-basic" style="scroll-margin-top: 4rem"></span>')
        quantity = st.number_input("비교 물량(kg)", min_value=0.0, value=None, key="future_pair_quantity")
        st.session_state.setdefault("future_pair_start", date.today())
        start = st.date_input("두 대안의 보관 시작일", value=None, key="future_pair_start")
        basis = st.selectbox("두 가정 단가의 수수료 기준", ["수수료 차감 전", "수수료 차감 후"],
                             index=None, key="future_pair_basis")
        st.caption("두 단가에 이미 판매 수수료를 뺀 금액을 쓰면 ‘수수료 차감 후’를 선택하세요.")
        gross = basis == "수수료 차감 전"
        left, right = st.columns(2)
        with left:
            a_days = _storage_period("A", 3)
        with right:
            b_days = _storage_period("B", 10)
        price_a = st.number_input("A안 가정 단가(원/kg)", min_value=0.0, value=None, key="future_pair_price_a")
        st.session_state.setdefault("future_pair_price_mode", "과거 변동으로 가정")
        mode = st.radio("B안 가정 단가 입력 방식", ["과거 변동으로 가정", "직접 입력"], index=None,
                        key="future_pair_price_mode")
        filters = None
        price_b = None
        drafts = st.session_state.setdefault("future_pair_price_drafts", {})
        if mode == "과거 변동으로 가정":
            with st.expander("과거 가격의 거래규격 입력", key="future_pair_history_open", on_change="rerun"):
                if a_days is not None and b_days is not None and b_days > a_days:
                    st.caption(f"두 출하일의 차이인 {b_days - a_days}일 간격의 과거 가격을 찾습니다.")
                filters = {}
                for key, label in HISTORY_FIELDS.items():
                    widget_key = f"future_pair_filter_{key}"
                    st.session_state.setdefault(widget_key, drafts.get(widget_key, ""))
                    value = st.text_input(f"과거가격 {label}", key=widget_key).strip()
                    drafts[widget_key] = value
                    if value:
                        filters[key] = value
        else:
            st.session_state.setdefault("future_pair_price_b", drafts.get("future_pair_price_b"))
            price_b = st.number_input("B안 가정 단가(원/kg)", min_value=0.0, value=None, key="future_pair_price_b")
            drafts["future_pair_price_b"] = price_b

        with st.expander("감모율과 비용 입력", key="future_pair_costs_open", on_change="rerun"):
            st.caption("비용이 없다고 확인한 항목에는 0을 입력하세요. 모르는 값은 비워 두고 확인하세요.")
            same_costs = st.checkbox("A와 B의 하루 보관비·별도 비용·수수료율이 같아요",
                                     key="future_pair_shared_costs", on_change=_toggle_shared_costs, args=(state,))
            left, right = st.columns(2)
            with left:
                option_a = _cost_fields("A", a_days, gross)
            with right:
                shared = None
                if same_costs:
                    shared = {field: option_a[field] for field in ("daily_storage_cost", "other_cost_krw", "fee_percent")}
                    st.caption("B안 비용에는 A안에 입력한 값을 적용합니다.")
                option_b = _cost_fields("B", b_days, gross, shared=shared)

        section = st.session_state.pop("future_pair_input_jump", None)
        if section in ("history", "costs"):
            st.html(
                f"""<script>
                requestAnimationFrame(() => {{
                    const target = document.querySelector(".st-key-future_pair_{section}_open");
                    if (!target) return;
                    target.style.scrollMarginTop = "4rem";
                    target.scrollIntoView({{block: "start"}});
                    target.querySelector("summary")?.focus({{preventScroll: true}});
                }});
                </script>""",
                unsafe_allow_javascript=True,
            )

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
        if st.button("미래 A/B 비교 계산", key="future_pair_calculate", type="primary"):
            state.replace(inputs)
            st.session_state.future_pair_card_version = state.version
            result = state.calculate()
            if result["status"] == "NEEDS_INPUT" and result.get("missing_fields"):
                st.session_state.future_pair_missing = result
            else:
                st.session_state.pop("future_pair_missing", None)
        else:
            result = state.current_result()
        if result is None:
            pending = st.session_state.get("future_pair_missing")
            if pending is None or pending["future_pair_version"] != state.version:
                st.session_state.pop("future_pair_missing", None)
                return None
            result = pending
        if result["status"] != "OK":
            st.warning(future_pair_feedback(result))
            if result["status"] == "NEEDS_INPUT":
                _show_missing_inputs(result)
            return None
        render_future_pair_result(result, "card")
        return result


def render_future_pair_result(result, location):
    entries = [{"tool": "compare_future_pair", "result": result}]
    if location == "card":
        st.markdown(render_report(entries))
    try:
        pdf = render_pdf_report(entries)
    except Exception:
        st.warning("미래 A/B PDF를 생성하지 못했습니다. 화면과 JSON으로 결과를 확인할 수 있습니다.")
    else:
        st.download_button("미래 A/B PDF 내려받기", pdf, "미래출하비교.pdf", "application/pdf",
                           key=f"future_pair_pdf_{location}", on_click="ignore")
    content = json.dumps({"format": "jeju-future-pair", "version": 1, "result": result}, ensure_ascii=False, indent=2)
    st.download_button("미래 A/B 결과 JSON 내려받기", content, "미래출하비교.json", "application/json",
                       key=f"future_pair_json_{location}", on_click="ignore")
    if location == "chat":
        with st.expander("가격 근거와 상세 조건 보기"):
            st.markdown(render_report(entries))
