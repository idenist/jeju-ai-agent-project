"""Data-backed selectors for actual auction markets and trade specifications."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

import streamlit as st


MARKET_FIELDS = ("market_code", "variety", "grade", "size", "package", "package_kg")
MARKET_LABELS = {
    "market_code": "시장",
    "variety": "품종",
    "grade": "등급",
    "size": "크기",
    "package": "포장 종류",
    "package_kg": "포장 중량(kg)",
}


@st.cache_data(show_spinner=False, max_entries=512)
def _market_catalog(filters_json: str, revision: tuple[str, ...]):
    from services.catalog import explore_data

    return explore_data("market", json.loads(filters_json), limit=1)


def _clear_later_fields(prefix: str, index: int, callback=None) -> None:
    st.session_state[f"{prefix}_edited"] = True
    for field in MARKET_FIELDS[index + 1:]:
        # None is an intentional reset. Removing the key would restore an old
        # committed selection on the next render.
        st.session_state[f"{prefix}_{field}"] = None
    if callback is not None:
        callback()


def market_name_map() -> dict[str, str]:
    from services.task_context import data_revision

    result = _market_catalog("{}", tuple(data_revision()))
    return {str(item["code"]): str(item["name"])
            for item in result.get("market_options", [])}


def render_market_spec_controls(prefix: str, current: dict | None = None, *, on_change=None) -> tuple[dict, bool]:
    """Render cascading selectors that only offer combinations in the price data."""
    from services.task_context import data_revision

    current = dict(current or {})
    if current.get("package_kg") not in (None, ""):
        try:
            current["package_kg"] = str(Decimal(str(current["package_kg"])).normalize())
        except InvalidOperation:
            current["package_kg"] = None
    source_key = f"{prefix}_source_spec"
    source_spec = {field: current.get(field) for field in MARKET_FIELDS}
    previous_source = st.session_state.get(source_key)
    edited = st.session_state.pop(f"{prefix}_edited", False)
    if current and not edited and previous_source is not None and previous_source != source_spec:
        for field in MARKET_FIELDS:
            st.session_state[f"{prefix}_{field}"] = source_spec[field]
    st.session_state[source_key] = source_spec
    revision = tuple(data_revision())
    market_result = _market_catalog("{}", revision)
    if market_result.get("status") != "OK":
        st.info("거래규격 선택 자료를 읽지 못했습니다. 자료 상태를 확인해 주세요.")
        return {}, False

    market_names = {str(item["code"]): str(item["name"])
                    for item in market_result.get("market_options", [])}
    selected: dict = {}
    market_key = f"{prefix}_market_code"
    old_market = str(current.get("market_code")) if current.get("market_code") is not None else None
    if market_key not in st.session_state:
        st.session_state[market_key] = old_market if old_market in market_names else None
    elif st.session_state.get(market_key) not in [None, *market_names]:
        st.session_state.pop(market_key, None)
    market_code = st.selectbox(
        MARKET_LABELS["market_code"], [None, *market_names],
        format_func=lambda value: "선택해 주세요" if value is None else market_names.get(value, value),
        key=market_key, on_change=_clear_later_fields, args=(prefix, 0, on_change),
    )
    if market_code is not None:
        selected["market_code"] = market_code

    for index, field in enumerate(MARKET_FIELDS[1:], start=1):
        filters_json = json.dumps(selected, ensure_ascii=False, sort_keys=True)
        catalog = _market_catalog(filters_json, revision)
        values = catalog.get("available_filters", {}).get(field, [])
        key = f"{prefix}_{field}"
        previous_field = MARKET_FIELDS[index - 1]
        old = current.get(field) if selected.get(previous_field) not in (None, "") else None
        if field == "package_kg" and old is not None:
            old = str(old)
        if selected.get(previous_field) in (None, ""):
            st.session_state.pop(key, None)
        if key not in st.session_state:
            st.session_state[key] = old if old in values else None
        elif st.session_state.get(key) not in [None, *values]:
            st.session_state.pop(key, None)
        preceding_missing = selected.get(previous_field) in (None, "")
        value_label = (lambda item, field=field, preceding_missing=preceding_missing: ("먼저 앞 조건을 선택해 주세요" if preceding_missing else "선택해 주세요") if item is None
                       else f"{item}kg" if field == "package_kg" else str(item))
        value = st.selectbox(
            MARKET_LABELS[field], [None, *values],
            format_func=value_label,
            key=key, on_change=_clear_later_fields, args=(prefix, index, on_change),
            disabled=selected.get(previous_field) in (None, ""),
        )
        if value is not None:
            selected[field] = value

    if selected:
        selected["trade_type"] = "경매"
    complete = all(selected.get(field) not in (None, "") for field in MARKET_FIELDS)
    if not market_names:
        st.info("현재 가격자료에 시장 정보가 없습니다.")
    elif not complete:
        st.caption("시장과 상품 규격은 확보된 경매자료에 실제 있는 항목만 선택할 수 있어요.")
    return selected, complete
