"""A/B economics + multi-season market reference, in the existing result flow.

Never chooses a farm's variety automatically. Reuse a specifically submitted
market lookup or ask for the actual comparison specification inline once.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
import streamlit as st
from services.market_decision import (
    evaluate_market_decision, market_query_from_explicit_selection,
)
from services.seasonal_prices import seasonal_price_revision

FIELDS = ("market_code", "variety", "grade", "size", "package", "package_kg", "trade_type")
LABELS = ("시장", "품종", "등급", "크기", "포장", "상자중량(kg)", "거래방식")
STATUSES = {
    "NOT_PREPARED": "시즌 가공본 없음", "NO_DATA": "해당 규격 거래 없음",
    "NO_CLEAN_DAYS": "품질 경고로 비교 불가", "AMBIGUOUS_CODES": "분류 코드 모호",
    "DATA_ERROR": "가격 자료 오류", "INVALID_CALENDAR_DATE": "존재하지 않는 날짜",
}


def _fmt(value, digits=1):
    return f"{Decimal(str(value)):,.{digits}f}" if value is not None else "-"


def _fmt_spec(spec):
    market = spec.get("market_code")
    if market:
        from ui.market_inputs import market_name_map
        market = market_name_map().get(str(market), market)
    values = [market, *(spec.get(key) for key in FIELDS[1:])]
    return " · ".join(f"{label} {value}" for value, label in zip(values, LABELS))


def _spec_form(location, current):
    """Only displayed for missing specs or when the user explicitly asks to edit."""
    old = current or {}
    from ui.market_inputs import render_market_spec_controls

    selection, complete = render_market_spec_controls(f"market_decision_spec_{location}", old)
    window_key = f"market_decision_spec_{location}_window"
    window_values = [0, 3, 7, 14]
    old_window = old.get("window_days", 7)
    if window_key not in st.session_state:
        st.session_state[window_key] = old_window if old_window in window_values else 7
    window = st.selectbox("출하 예정일 전후 범위", window_values,
                          format_func=lambda n: "해당 날짜" if n == 0 else f"±{n}일",
                          key=window_key)
    submitted = st.button("이 거래규격으로 시장 근거 연결", disabled=not complete,
                          key=f"market_decision_spec_submit_{location}")
    if not submitted:
        def same_value(key):
            a, b = selection.get(key), old.get(key)
            if key == "package_kg" and a not in (None, "") and b not in (None, ""):
                return Decimal(str(a)) == Decimal(str(b))
            return a == b
        fields_changed = (any(not same_value(key) for key in FIELDS)
                          or window != old.get("window_days", 7)) if old else any(
                              selection.get(key) not in (None, "") for key in FIELDS)
        return None, fields_changed
    selection["window_days"] = window
    if any(not selection.get(k) for k in FIELDS):
        st.warning("실제 상품과 같은 거래규격을 선택해 주세요.")
        return None, False
    return selection, False


def _display(result):
    if result["status"] not in ("OK", "NO_CLEAN_DAYS"):
        st.warning(result.get("message", "조회에 실패했습니다."))
        return
    comparison = result["comparison"]
    decision = comparison["decision_under_assumptions"]
    def date_label(value):
        if not value:
            return "날짜 미확인"
        parsed = date.fromisoformat(value)
        return f"{parsed.month}월 {parsed.day}일 출하"
    earlier = date_label(comparison.get("option_a_date"))
    later = date_label(comparison.get("option_b_date"))
    if decision == "B_ASSUMED_HIGHER":
        st.markdown(f"**입력한 가정에서는 {later}가 {earlier}보다 {_fmt(comparison['difference_vs_a_krw'],0)}원 유리합니다.**")
    elif decision == "A_ASSUMED_HIGHER":
        st.markdown(f"**입력한 가정에서는 {earlier}가 {later}보다 {_fmt(-Decimal(comparison['difference_vs_a_krw']),0)}원 유리합니다.**")
    elif decision == "ASSUMED_TIE":
        st.markdown(f"**입력한 가정에서는 {earlier}와 {later}의 비교금액이 같습니다.**")
    else:
        st.markdown("**입력한 가정만으로는 두 출하 날짜의 유불리를 정할 수 없습니다.**")
    st.markdown(f"{later}가 {earlier}보다 손해 보지 않으려면 최소 **{_fmt(comparison['minimum_whole_krw_per_kg'],0)}원/kg**이 필요합니다.")
    evidence = result["market_evidence"]
    labels = []
    for item in evidence.get("seasons", []):
        if item["status"] == "OK":
            status = "부분 관측기간" if not item.get("window_complete") else "조회됨"
        else:
            status = STATUSES.get(item["status"], item["status"])
        clean_days = item.get("days_clean")
        above_days = item.get("days_above")
        labels.append({"시즌": item["season"],
                       "손익분기 가격 이상이었던 날": f"{above_days} / {clean_days}일"
                       if above_days is not None and clean_days is not None else "-",
                       "자료 상태": status})
    st.dataframe(labels, hide_index=True, use_container_width=True)
    st.caption("같은 규격의 과거 일별 평균 경락가격을 센 결과입니다. 앞으로 그 가격을 받을 확률은 아닙니다.")
    with st.expander("거래량 기준 상세 보기"):
        detail_rows = [{"시즌": item["season"],
                        "손익분기 가격 이상 날짜의 거래량 비중":
                            f"{_fmt(item['kg_share_percent'])}%" if item.get("kg_share_percent") is not None else "-"}
                       for item in evidence.get("seasons", [])]
        st.dataframe(detail_rows, hide_index=True, use_container_width=True)
    st.markdown("**출하 전에 확인할 것:** B안 실제 제시가격, 감모율·보관비, 수수료·공제 항목, 저장 및 출하일 변경 가능 여부")


def render_threshold_evidence(pair_result: dict, location: str):
    """Retains the previous call signature used by the installed 2nd-stage patch."""
    storage_key = f"market_decision_latest_{location}"
    if not isinstance(pair_result, dict) or pair_result.get("status") != "OK":
        st.session_state.pop(storage_key, None)
        return
    if pair_result.get("suppress_market_evidence"):
        st.session_state.pop(storage_key, None)
        with st.container(border=True):
            st.markdown("**다년도 시장 근거**")
            if pair_result.get("historical_price_lookup") == "used":
                st.info("과거 가격쌍은 위 시나리오에 반영했습니다. 미확인 비용이 있어 별도 다년도 시장 비교는 연결하지 않았습니다.")
            else:
                st.info("이 비교에서는 과거 가격과 시장 자료를 조회하지 않았습니다.")
        return
    if pair_result.get("price_basis") != "gross":
        st.session_state.pop(storage_key, None)
        with st.container(border=True):
            st.markdown("**시장 참고가격과 손익분기 가격 연결**")
            st.info("손익분기 가격이 수수료 차감 후 단가입니다. 차감 전 경락가격과 직접 비교하지 않습니다.")
        return
    last = market_query_from_explicit_selection(st.session_state.get("seasonal_prices_result"))
    own_key = "market_decision_custom_spec"
    # A newly submitted lookup replaces a previously overridden inline spec.
    lookup_key = (json.dumps(last, sort_keys=True, default=str) if last else None)
    stamp_key = "market_decision_last_lookup_key"
    if lookup_key != st.session_state.get(stamp_key):
        st.session_state.pop(own_key, None)
        st.session_state[stamp_key] = lookup_key
    spec = st.session_state.get(own_key) or last
    with st.container(border=True):
        st.markdown("**출하 손익과 다년도 시장 근거**")
        if spec:
            st.caption("시장 참고 규격 (실제 출하 상품과 일치 여부를 확인하세요): " + _fmt_spec(spec))
        else:
            st.info("다년도 시장 비교에 사용할 거래규격을 아래에서 선택하세요.")
        with st.expander("시장 규격 설정·수정", expanded=spec is None):
            changed, draft_changed = _spec_form(location, spec)
            if changed is not None:
                st.session_state[own_key] = spec = changed
        if draft_changed:
            st.session_state.pop(storage_key, None)
            st.session_state.pop(f"market_decision_result_{location}", None)
            st.info("거래규격 선택이 바뀌었습니다. 새 규격으로 시장 근거를 다시 연결해 주세요.")
            return
        if not spec:
            st.session_state.pop(storage_key, None)
            return
        # Cached by economics + explicit spec + local season-file revision. The
        # default-price assumptions are never treated as actual auction evidence.
        key = hashlib.sha256(json.dumps({
            "pair": pair_result, "spec": spec, "sources": seasonal_price_revision(),
        }, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
        cache_key = f"market_decision_result_{location}"
        cached = st.session_state.get(cache_key)
        if not isinstance(cached, dict) or cached.get("key") != key:
            with st.spinner("각 시즌의 동일 규격 경매가격을 비교합니다"):
                result = evaluate_market_decision(pair_result, spec)
            conditions = (result.get("market_evidence") or {}).get("conditions") or result.get("query")
            if isinstance(conditions, dict):
                from ui.market_inputs import market_name_map
                display_conditions = dict(conditions)
                code = display_conditions.get("market_code")
                if code:
                    display_conditions["market_code"] = market_name_map().get(str(code), code)
                result["display_conditions"] = display_conditions
            st.session_state[cache_key] = {"key": key, "result": result}
        else:
            result = cached["result"]
        # Keep the pure joined record available to later PDF/JSON integration,
        # but do not mutate FuturePairState's authoritative economic result.
        st.session_state[storage_key] = {"pair_fingerprint": key, "result": result}
        _display(result)
