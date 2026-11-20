"""Direct-entry view for comparing two future shipment dates."""
from datetime import date, timedelta
from decimal import Decimal

import streamlit as st

from agents.future_pair_state import HISTORY_FIELDS, MAX_RATE_PERCENT, OPTION_LABELS, future_pair_feedback
from agents.box_price import BoxPrice
from ui.pdf_report import render_pdf_report
from presenters.reporting import manual_price_warning, render_report


def _storage_period(side, start, default_days, use_dates):
    key = f"future_pair_{side}"
    if use_dates:
        date_key = f"{key}_sale_date"
        st.session_state.setdefault(date_key, start + timedelta(days=default_days) if start and default_days else None)
        sale = st.date_input(f"{side}안 출하일", value=None, key=date_key)
        if not (sale and start):
            return None
        days = (sale - start).days
        if not 1 <= days <= 365:
            st.warning("출하일은 보관 시작 다음 날부터 365일 안에서 선택해 주세요.")
            return None
        st.caption(f"보관 시작일로부터 {days}일")
        return days
    st.session_state.setdefault(f"{key}_days", default_days)
    return st.number_input(f"{side}안 보관일", 1, 365, value=None, key=f"{key}_days")


def _quantity(previous):
    tons = st.session_state.get("future_pair_quantity_unit") == "톤"
    key = "future_pair_quantity_tons" if tons else "future_pair_quantity"
    st.session_state.setdefault(key, float(previous) / (1000 if tons else 1) if previous is not None else None)
    amount = st.number_input("비교 물량(톤)" if tons else "비교 물량(kg)", min_value=0.0,
                             value=None, format="%.6f" if tons else None, key=key)
    if amount is not None and tons:
        amount = float(Decimal(str(amount)) * 1000)
        st.caption(f"비교에 적용할 물량은 {amount:,.6g}kg이에요.")
    return amount


def _manual_price(side, drafts):
    key = f"future_pair_price_{side.lower()}"
    unit_key = f"{key}_unit"
    amount_key, weight_key = f"{key}_box_amount", f"{key}_box_weight"
    unit = st.session_state.get(unit_key, drafts.get(unit_key, "원/kg"))
    if unit == "원/kg":
        st.session_state.setdefault(key, drafts.get(key))
        price = st.number_input(f"{side}안 가정 단가(원/kg)", min_value=0.0, value=None, key=key)
        if price != drafts.get(key) and (weight := drafts.get(weight_key)):
            drafts[amount_key] = float(Decimal(str(price)) * Decimal(str(weight))) if price is not None else None
            st.session_state.pop(amount_key, None)
        drafts[key] = price
        return price

    st.session_state.setdefault(amount_key, drafts.get(amount_key))
    st.session_state.setdefault(weight_key, drafts.get(weight_key))
    amount_col, weight_col = st.columns(2)
    with amount_col:
        amount = st.number_input(f"{side}안 상자당 가격(원)", min_value=0.0, value=None, key=amount_key)
    with weight_col:
        weight = st.number_input(f"{side}안 판매 상자 무게(kg)", min_value=0.001, max_value=1000.0,
                                 value=None, format="%.3f", key=weight_key)
    drafts[amount_key], drafts[weight_key] = amount, weight
    if amount is None or weight is None:
        st.caption("실제로 판매할 상자 한 개의 무게와 가격을 입력해 주세요.")
        return None
    price = float(BoxPrice(Decimal(str(amount)), side, Decimal(str(weight))).unit_price)
    st.caption(f"{side}안 비교에는 {price:,.2f}원/kg을 적용해요.")
    drafts[key] = price
    return price


def _period_loss(side, days, quantity, use_sale_weight):
    key = f"future_pair_{side}"
    period = f"{days}일" if days is not None else "기간"
    drafts = st.session_state.setdefault("future_pair_loss_drafts", {})
    previous = drafts.get((side, days))
    if use_sale_weight:
        weight_key = f"{key}_sale_weight_{days}"
        ready = days is not None and quantity is not None and quantity > 0
        retained_weight = float(Decimal(str(quantity)) * (1 - Decimal(str(previous)) / 100)) if ready and previous is not None else None
        st.session_state.setdefault(weight_key, retained_weight)
        sale_weight = st.number_input(f"{side}안 {period} 후 판매 가능량(kg)", min_value=0.0, value=None,
                                      key=weight_key, disabled=not ready)
        loss = None
        if ready and sale_weight is not None:
            rate = float((1 - Decimal(str(sale_weight)) / Decimal(str(quantity))) * 100)
            if 0 <= rate <= MAX_RATE_PERCENT:
                loss = previous if sale_weight == retained_weight else rate
                st.caption(f"이 무게에 해당하는 기간 전체 감모율은 {loss:.4g}%예요.")
            else:
                st.warning("판매 가능량은 전체 물량을 넘을 수 없으며, 이 비교에는 판매할 수 있는 물량이 필요해요.")
    else:
        loss_key = f"{key}_loss_{days}"
        st.session_state.setdefault(loss_key, previous)
        loss = st.number_input(f"{side}안 {period} 전체 감모율(%)", min_value=0.0, max_value=MAX_RATE_PERCENT,
                               value=None, key=loss_key, disabled=days is None,
                               help="보관 기록에서 줄어든 판매 가능 무게를 확인하세요. 100kg 중 4kg이 줄었으면 4를 입력하세요.")
    drafts[side, days] = loss
    return loss


def _fee_input(side):
    fee_key = f"future_pair_{side}_fee"
    fee_drafts = st.session_state.setdefault("future_pair_fee_drafts", {})
    st.session_state.setdefault(fee_key, fee_drafts.get(side))
    fee = st.number_input(f"{side}안 수수료율(%)", min_value=0.0, max_value=MAX_RATE_PERCENT,
                          value=None, key=fee_key, help="출하처 정산서나 약정에서 확인할 수 있어요.")
    fee_drafts[side] = fee
    return fee


def _storage_costs(side):
    key = f"future_pair_{side}"
    storage = st.number_input(f"{side}안 전체 물량 하루 보관비(원)", min_value=0.0, value=None,
                              key=f"{key}_storage", help="비교 물량 전체의 하루 보관비를 저장고 요금표나 견적에서 확인하세요.")
    other = st.number_input(f"{side}안 별도 비용 합계(원)", min_value=0.0, value=None, key=f"{key}_other",
                            help="단가에 포함되지 않은 비용만 입력하세요. 수수료와 보관비는 별도 입력합니다.")
    return {"daily_storage_cost": storage, "other_cost_krw": other}


def _sync_inputs(state):
    if st.session_state.get("future_pair_card_version", 0) == state.version:
        return False
    values = state.inputs
    st.session_state.pop("future_pair_separate_costs", None)
    if any(values["option_a"][field] != values["option_b"][field]
           for field in ("daily_storage_cost", "other_cost_krw", "fee_percent")):
        st.session_state.future_pair_shared_costs = False
    st.session_state.future_pair_applied_shared_costs = st.session_state.get("future_pair_shared_costs", False)
    for key in list(st.session_state):
        if key.startswith(("future_pair_A_loss_", "future_pair_B_loss_",
                           "future_pair_A_sale_weight_", "future_pair_B_sale_weight_")):
            del st.session_state[key]
    st.session_state.future_pair_loss_drafts = {}
    st.session_state.future_pair_start = date.fromisoformat(values["start_date"]) if values["start_date"] else None
    st.session_state.future_pair_basis = {"gross": "수수료 차감 전", "commission_net": "수수료 차감 후"}.get(values["price_basis"])
    result = state.current_result()
    if result and result.get("threshold_only") and values["history_filters"] is None:
        st.session_state.future_pair_goal = "참고 손익분기"
    elif values["history_filters"] is not None or values["price_b"] is not None:
        st.session_state.future_pair_goal = "금액 비교"
    st.session_state.future_pair_price_mode = "과거 변동으로 가정" if values["history_filters"] is not None else "직접 입력"
    if not (result and result.get("threshold_only") and values["history_filters"] is None):
        st.session_state.future_pair_amount_mode = st.session_state.future_pair_price_mode
    for field in ("quantity", "price_a"):
        value = values[field]
        st.session_state[f"future_pair_{field}"] = float(value) if value is not None else None
    drafts = st.session_state.setdefault("future_pair_price_drafts", {})
    st.session_state.pop("future_pair_quantity_tons", None)
    for side in ("a", "b"):
        prefix = f"future_pair_price_{side}_"
        for suffix in ("unit", "box_amount", "box_weight"):
            st.session_state.pop(prefix + suffix, None)
            drafts.pop(prefix + suffix, None)
    drafts["future_pair_price_a"] = st.session_state.future_pair_price_a
    if values["history_filters"] is None:
        value = values["price_b"]
        if value is not None or not (result and result.get("threshold_only")):
            drafts["future_pair_price_b"] = float(value) if value is not None else None
        st.session_state.future_pair_price_b = drafts.get("future_pair_price_b")
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
        st.session_state.pop(f"{prefix}_sale_date", None)
        for field, suffix in (("loss_percent", f"loss_{days}"), ("daily_storage_cost", "storage"),
                              ("other_cost_krw", "other"), ("fee_percent", "fee")):
            value = option[field]
            st.session_state[f"{prefix}_{suffix}"] = float(value) if value is not None else None
        st.session_state.future_pair_loss_drafts[side, days] = st.session_state[f"{prefix}_loss_{days}"]
    st.session_state.future_pair_fee_drafts = {
        side: st.session_state[f"future_pair_{side}_fee"] for side in ("A", "B")
    }
    st.session_state.future_pair_card_version = state.version
    return True


def _toggle_shared_costs(state, same_costs):
    fields = {"daily_storage_cost": "storage", "other_cost_krw": "other"}
    if same_costs:
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


def _switch_to_manual_price():
    st.session_state.future_pair_goal = "금액 비교"
    st.session_state.future_pair_price_mode = "직접 입력"
    st.session_state.future_pair_price_b_unit = "원/kg"
    st.session_state.future_pair_input_jump = "manual_price"


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
    previous = st.session_state.get("future_pair_card_inputs")
    shown = state.inputs if synced else previous or state.inputs
    with st.container():
        goal = st.radio("무엇을 확인할까요?", ["금액 비교", "참고 손익분기"], horizontal=True,
                        key="future_pair_goal")
        reference = goal == "참고 손익분기"
        mode = None
        if reference:
            st.caption("나중 단가 없이 필요한 최소 가격을 확인해요. 보관비와 별도 비용은 알고 있을 때 반영할 수 있어요.")
        else:
            st.session_state.setdefault("future_pair_price_mode", st.session_state.get("future_pair_amount_mode", "직접 입력"))
            mode = st.radio("B안 가정 단가 입력 방식", ["직접 입력", "과거 변동으로 가정"], horizontal=True,
                            key="future_pair_price_mode")
            st.session_state.future_pair_amount_mode = mode
        drafts = st.session_state.setdefault("future_pair_price_drafts", {})
        with st.expander("입력 단위와 공통 비용 설정"):
            st.selectbox("물량 단위", ["kg", "톤"], key="future_pair_quantity_unit")
            use_dates = st.radio("출하 일정 입력 방식", ["보관일로 입력", "출하일로 입력"], horizontal=True,
                                 key="future_pair_schedule_mode") == "출하일로 입력"
            for side in ("A", "B") if mode == "직접 입력" else ("A",):
                unit_key = f"future_pair_price_{side.lower()}_unit"
                st.session_state.setdefault(unit_key, drafts.get(unit_key, "원/kg"))
                drafts[unit_key] = st.selectbox(f"{side}안 단가 단위", ["원/kg", "원/상자"], key=unit_key)
            use_sale_weight = st.radio(
                "보관 중 줄어드는 양의 입력 방식", ["감모율(%)", "판매 가능량(kg)"],
                horizontal=True, key="future_pair_loss_mode",
            ) == "판매 가능량(kg)"
            same_costs = st.checkbox("A와 B의 하루 보관비·별도 비용·수수료율이 같아요",
                                     key="future_pair_shared_costs")
        if same_costs != st.session_state.get("future_pair_applied_shared_costs", False):
            _toggle_shared_costs(state, same_costs)
            st.session_state.future_pair_applied_shared_costs = same_costs
        st.html('<span id="future-pair-basic" style="scroll-margin-top: 4rem"></span>')
        quantity = _quantity(shown["quantity"])
        st.session_state.setdefault("future_pair_start", date.today())
        start = st.date_input("두 대안의 보관 시작일", value=None, key="future_pair_start")
        basis = st.selectbox("단가의 수수료 기준", ["수수료 차감 전", "수수료 차감 후"],
                             index=None, key="future_pair_basis",
                             help="이미 수수료를 뺀 단가를 비교하면 ‘수수료 차감 후’를 선택하세요.")
        gross = basis == "수수료 차감 전"
        left, right = st.columns(2)
        with left:
            a_days = _storage_period("A", start, shown["option_a"]["delay_days"] if previous or synced else 3, use_dates)
        with right:
            b_days = _storage_period("B", start, shown["option_b"]["delay_days"] if previous or synced else 10, use_dates)
        price_a = _manual_price("A", drafts)
        filters = None
        price_b = None
        if mode == "과거 변동으로 가정":
            with st.expander("과거 가격의 거래규격 입력", expanded=True,
                             key="future_pair_history_open", on_change="rerun"):
                if a_days is not None and b_days is not None and b_days > a_days:
                    st.caption(f"두 출하일의 차이인 {b_days - a_days}일 간격의 과거 가격을 찾습니다.")
                from ui.market_inputs import render_market_spec_controls
                current = {field: drafts.get(f"future_pair_filter_{field}")
                           for field in HISTORY_FIELDS if drafts.get(f"future_pair_filter_{field}")}
                filters, _ = render_market_spec_controls("future_pair_filter", current)
                for field in HISTORY_FIELDS:
                    widget_key = f"future_pair_filter_{field}"
                    drafts[widget_key] = filters.get(field, "")
        elif mode == "직접 입력":
            price_b = _manual_price("B", drafts)
            st.caption("나중 단가를 모르면 위에서 ‘참고 손익분기’를 선택할 수 있어요.")
            if warning := manual_price_warning(price_a, price_b):
                st.warning(warning)

        st.markdown("**감모율과 수수료**" if gross else "**보관 중 줄어드는 양**")
        left, right = st.columns(2)
        with left:
            loss_a = _period_loss("A", a_days, quantity, use_sale_weight)
            fee_a = _fee_input("A") if gross else None
        with right:
            loss_b = _period_loss("B", b_days, quantity, use_sale_weight)
            fee_b = fee_a if same_costs else _fee_input("B") if gross else None

        costs_section = (st.expander("보관비와 별도 비용 · 선택", key="future_pair_costs_open", on_change="rerun")
                         if reference else st.container(key="future_pair_costs_open"))
        with costs_section:
            if not reference:
                st.markdown("**보관비와 별도 비용**")
            st.caption("비용이 없다고 확인한 항목에는 0을 입력하세요.")
            left, right = st.columns(2)
            with left:
                costs_a = _storage_costs("A")
            with right:
                if same_costs:
                    st.caption("B안에는 A안과 같은 비용을 적용해요.")
                costs_b = costs_a if same_costs else _storage_costs("B")
        option_a = {**costs_a, "delay_days": a_days, "loss_percent": loss_a, "fee_percent": fee_a}
        option_b = {**costs_b, "delay_days": b_days, "loss_percent": loss_b, "fee_percent": fee_b}
        if reference and any(value is not None for value in (*costs_a.values(), *costs_b.values())):
            st.caption("입력한 보관비와 별도 비용을 참고 손익분기에 반영해요.")

        section = st.session_state.pop("future_pair_input_jump", None)
        if section in ("history", "costs", "manual_price"):
            target_key = "price_b" if section == "manual_price" else f"{section}_open"
            focus_selector = "input" if section == "manual_price" or (section == "costs" and not reference) else "summary"
            st.html(
                f"""<script>
                requestAnimationFrame(() => {{
                    const target = document.querySelector(".st-key-future_pair_{target_key}");
                    if (!target) return;
                    target.style.scrollMarginTop = "4rem";
                    target.scrollIntoView({{block: "start"}});
                    target.querySelector("{focus_selector}")?.focus({{preventScroll: true}});
                }});
                </script>""",
                unsafe_allow_javascript=True,
            )

        inputs = {"quantity": quantity, "price_basis": ("gross" if gross else "commission_net") if basis else None,
                  "start_date": start.isoformat() if start else None, "option_a": option_a, "option_b": option_b,
                  "price_a": price_a, "price_b": price_b, "history_filters": filters}
        current = state.current_result()
        if not synced and previous is not None and inputs != previous:
            state.replace(inputs)
            st.session_state.future_pair_card_version = state.version
            if current is not None:
                st.info("비교 조건이나 자료가 바뀌었습니다. 현재 입력으로 다시 계산해 주세요.")
        st.session_state.future_pair_card_inputs = inputs
        calculate_label = "참고 손익분기 계산" if reference else "미래 A/B 비교 계산"
        if st.button(calculate_label, key="future_pair_calculate", type="primary"):
            state.replace(inputs)
            st.session_state.future_pair_card_version = state.version
            result = state.calculate(allow_reference=reference)
            if result.get("data_gap"):
                state.pending = result
            if result["status"] == "NEEDS_INPUT" and result.get("missing_fields"):
                st.session_state.future_pair_missing = result
            else:
                st.session_state.pop("future_pair_missing", None)
        else:
            result = state.current_result()
        if result is None:
            pending = st.session_state.get("future_pair_missing") or state.pending
            if pending is None or pending["future_pair_version"] != state.version:
                st.session_state.pop("future_pair_missing", None)
                return None
            result = pending
        if result["status"] != "OK":
            st.warning(future_pair_feedback(result))
            if result.get("manual_price_required"):
                st.button("나중 출하 단가 직접 입력", key="future_pair_use_manual_price",
                          on_click=_switch_to_manual_price)
            if result["status"] == "NEEDS_INPUT":
                _show_missing_inputs(result)
            if result.get("data_gap"):
                from ui.price_gap_ui import render_price_gap_downloads

                render_price_gap_downloads(result, "compare_future_pair", "pair_card")
            return None
        render_future_pair_result(result, "card")
        return result


def render_future_pair_result(result, location):
    # Build the market record before creating download payloads. The market panel
    # refreshes its local cache when A/B inputs, trade specs, or season files change.
    from presenters.market_export import export_market_record, report_entries
    from ui.seasonal_threshold_ui import render_threshold_evidence

    base_entries = [{"tool": "compare_future_pair", "result": result}]
    if location == "chat":
        with st.container(border=True):
            st.markdown("**감귤 출하 비교 보고서**")
            render_threshold_evidence(result, location)
            market = export_market_record(result, st.session_state.get(f"market_decision_latest_{location}"))
            entries = report_entries(result, market)
            _render_future_pair_pdf(entries, location)
            with st.expander("보고서 미리보기"):
                st.markdown(render_report(entries))
            with st.expander("상세 · JSON"):
                _render_future_pair_json(result, location, market)
    else:
        st.markdown(render_report(base_entries))
        render_threshold_evidence(result, location)
        market = export_market_record(result, st.session_state.get(f"market_decision_latest_{location}"))
        entries = report_entries(result, market)
        _render_future_pair_pdf(entries, location)
        _render_future_pair_json(result, location, market)


def _render_future_pair_pdf(entries, location):
    try:
        pdf = render_pdf_report(entries)
    except Exception:
        st.warning("미래 A/B PDF를 생성하지 못했습니다. 화면과 JSON으로 결과를 확인할 수 있습니다.")
    else:
        st.download_button("두 출하일 비교 PDF 저장", pdf, "미래출하비교.pdf", "application/pdf",
                           key=f"future_pair_pdf_{location}", on_click="ignore")


def _render_future_pair_json(result, location, market_result):
    from presenters.market_export import export_json

    content = export_json(result, market_result)
    st.caption("결과 JSON은 당시 계산을 보관합니다. 비교 조건은 사이드바에서 따로 저장할 수 있어요.")
    st.download_button("미래 A/B 결과 JSON 내려받기", content, "미래출하비교.json", "application/json",
                       key=f"future_pair_json_{location}", on_click="ignore")
