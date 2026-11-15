"""A/B economics + multi-season market reference, in the existing result flow.

Never chooses a farm's variety automatically. Reuse a specifically submitted
market lookup or ask for the actual comparison specification inline once.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import streamlit as st
from agents.common.market_decision import (
    evaluate_market_decision, market_query_from_explicit_selection,
)
from agents.common.seasonal_prices import PROJECT_ROOT, SEASONS

FIELDS = ("market_code", "variety", "grade", "size", "package", "package_kg", "trade_type")
LABELS = ("시장", "품종", "등급", "크기", "포장", "상자중량(kg)", "거래방식")
STATUSES = {
    "NOT_PREPARED": "시즌 가공본 없음", "NO_DATA": "해당 규격 거래 없음",
    "NO_CLEAN_DAYS": "품질 경고로 비교 불가", "AMBIGUOUS_CODES": "분류 코드 모호",
    "DATA_ERROR": "가격 자료 오류", "INVALID_CALENDAR_DATE": "존재하지 않는 날짜",
}


def _fmt(value, digits=1):
    return f"{Decimal(str(value)):,.{digits}f}" if value is not None else "-"


def _source_stamp(root: Path = PROJECT_ROOT):
    """Invalidate prior market evidence when season CSV files change."""
    signature = []
    for season in SEASONS:
        path = Path(root) / "data/seasons" / season / "daily_prices.csv"
        try:
            stat = path.stat()
            signature.append((season, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            signature.append((season, None, None))
    return signature


def _fmt_spec(spec):
    return " · ".join(f"{label} {spec.get(key)}" for key, label in zip(FIELDS, LABELS))


def _spec_form(location, current):
    """Only displayed for missing specs or when the user explicitly asks to edit."""
    old = current or {}
    with st.form(f"market_decision_spec_{location}"):
        col1, col2, col3 = st.columns(3)
        market = col1.text_input("시장코드", value=str(old.get("market_code", "110001")))
        trade = col2.selectbox("거래방식", ["경매"])
        variety = col3.text_input("품종", value=str(old.get("variety", "")))
        col1, col2, col3, col4 = st.columns(4)
        grade = col1.text_input("등급", value=str(old.get("grade", "")))
        size = col2.text_input("크기", value=str(old.get("size", "")))
        package = col3.text_input("포장", value=str(old.get("package", "")))
        kg = col4.number_input("상자중량(kg)", min_value=0.001,
                               value=float(old["package_kg"]) if old.get("package_kg") else 5.0)
        window = st.selectbox("출하 예정일 전후 범위", [0, 3, 7, 14],
                              index=[0, 3, 7, 14].index(old.get("window_days", 7))
                              if old.get("window_days", 7) in [0, 3, 7, 14] else 2,
                              format_func=lambda n: "해당 날짜" if n == 0 else f"±{n}일")
        submitted = st.form_submit_button("이 거래규격으로 시장 근거 연결")
    if not submitted:
        return None
    selection = {"market_code": market.strip(), "trade_type": trade,
                 "variety": variety.strip(), "grade": grade.strip(),
                 "size": size.strip(), "package": package.strip(),
                 "package_kg": kg, "window_days": window}
    if any(not selection.get(k) for k in FIELDS):
        st.warning("거래규격의 모든 항목을 확인해 주세요. 실제 출하 상품과 동일한 조건이어야 합니다.")
        return None
    return selection


def _display(result):
    if result["status"] not in ("OK", "NO_CLEAN_DAYS"):
        st.warning(result.get("message", "조회에 실패했습니다."))
        return
    comparison = result["comparison"]
    decision = comparison["decision_under_assumptions"]
    if decision == "B_ASSUMED_HIGHER":
        st.markdown(f"**입력한 단가·비용 가정에서는 B안이 {_fmt(comparison['difference_vs_a_krw'],0)}원 유리합니다.**")
    elif decision == "A_ASSUMED_HIGHER":
        st.markdown(f"**입력한 단가·비용 가정에서는 A안이 {_fmt(-Decimal(comparison['difference_vs_a_krw']),0)}원 유리합니다.**")
    elif decision == "ASSUMED_TIE":
        st.markdown("**입력한 단가·비용 가정에서 두 안의 비교금액은 같습니다.**")
    else:
        st.markdown("**여러 과거 가격 가정의 유불리는 위 손익 표에서 확인하세요.**")
    st.markdown(f"B안이 A안 이상이 되려면 **{_fmt(comparison['minimum_whole_krw_per_kg'],0)}원/kg**이 필요합니다.")
    evidence = result["market_evidence"]
    labels = []
    for item in evidence.get("seasons", []):
        if item["status"] == "OK":
            status = "부분 관측기간" if not item.get("window_complete") else "조회됨"
        else:
            status = STATUSES.get(item["status"], item["status"])
        labels.append({"시즌": item["season"], "정상 관측일": item["days_clean"],
                       "기준 이상 일수": item["days_above"],
                       "해당 일수 비율": f"{_fmt(item['daily_share_percent'])}%" if item["daily_share_percent"] is not None else "-",
                       "해당 날짜 거래중량 비율": f"{_fmt(item['kg_share_percent'])}%" if item["kg_share_percent"] is not None else "-",
                       "상태": status})
    st.dataframe(labels, hide_index=True, use_container_width=True)
    st.caption("과거 각 날짜의 동일 등록규격 평균 경락가격 기준입니다. 미래가격 달성 확률이나 개별 농가의 수취가격이 아닙니다.")
    st.markdown("**출하 전에 확인할 것:** B안 실제 제시가격, 감모율·보관비, 수수료·공제 항목, 저장 및 출하일 변경 가능 여부")


def render_threshold_evidence(pair_result: dict, location: str):
    """Retains the previous call signature used by the installed 2nd-stage patch."""
    storage_key = f"market_decision_latest_{location}"
    if not isinstance(pair_result, dict) or pair_result.get("status") != "OK":
        st.session_state.pop(storage_key, None)
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
            st.info("비교할 상품의 거래규격이 없습니다. 다른 탭으로 이동하지 않고 여기서 입력하세요.")
        with st.expander("시장 규격 설정·수정", expanded=spec is None):
            changed = _spec_form(location, spec)
            if changed is not None:
                st.session_state[own_key] = spec = changed
        if not spec:
            st.session_state.pop(storage_key, None)
            return
        # Cached by economics + explicit spec + local season-file revision. The
        # default-price assumptions are never treated as actual auction evidence.
        key = hashlib.sha256(json.dumps({
            "pair": pair_result, "spec": spec, "sources": _source_stamp(),
        }, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
        cache_key = f"market_decision_result_{location}"
        cached = st.session_state.get(cache_key)
        if not isinstance(cached, dict) or cached.get("key") != key:
            with st.spinner("각 시즌의 동일 규격 경매가격을 비교합니다"):
                result = evaluate_market_decision(pair_result, spec)
            st.session_state[cache_key] = {"key": key, "result": result}
        else:
            result = cached["result"]
        # Keep the pure joined record available to later PDF/JSON integration,
        # but do not mutate FuturePairState's authoritative economic result.
        st.session_state[storage_key] = {"pair_fingerprint": key, "result": result}
        _display(result)
