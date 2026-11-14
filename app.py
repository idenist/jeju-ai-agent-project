"""Shared conversational/card workspace. CSV tools and PDF work without a model key."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from time import monotonic
from datetime import date, timedelta
import streamlit as st

from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.reporting import render_report, render_result, FIELD_NAMES
from agents.juneok.presentation import connection_label, render_dialogue
from agents.juneok.ui_workflow import COST_LABELS, INPUT_CONTEXTS, form_snapshot, condition_rows
from agents.juneok.workspace import SharedWorkspace, card_values

_REPORT_REQUEST = re.compile(
    r"(?:방금|아까|직전|마지막|최근)\s*(?:한\s*)?(?:출하\s*)?비교\s*(?:결과)?\s*(?:를|을)?\s*"
    r"(?:(?:보고서\s*(?:로|를)?\s*)?(?:pdf|피디에프)\s*(?:파일)?\s*(?:로|를)?|보고서\s*(?:로|를)?)\s*"
    r"(?:다시\s*)?(?:줘(?:요)?|주세요|보여\s*줘(?:요)?|보여\s*주세요|받을\s*수\s*있나요|다운로드)?[.!?\s]*",
    re.IGNORECASE,
)


def _workspace():
    if "workspace" not in st.session_state:
        session = st.session_state.get("agent_session") or AgentSession(settings=Settings.load())
        st.session_state.workspace = SharedWorkspace(session)
        st.session_state.agent_session = session
    return st.session_state.workspace


def _card_dirty():
    saved = st.session_state.get("card_form_snapshot")
    return saved is not None and form_snapshot(st.session_state) != saved


def _sync_card(force=False):
    ws = _workspace()
    if force or "card_base_version" not in st.session_state or (st.session_state.card_base_version != ws.store.version and not _card_dirty()):
        values, ids = card_values(ws.store.snapshot())
        st.session_state.update(values)
        st.session_state.input_context = ws.input_context
        st.session_state.card_option_ids = ids
        st.session_state.card_base_version = ws.store.version
        st.session_state.card_form_snapshot = form_snapshot(st.session_state)
        st.session_state.pop("_period_change_notice", None)


def _load_demo():
    _workspace().load_demo()
    _sync_card(force=True)


def _reload_cards():
    _sync_card(force=True)


def _on_period_edit(index):
    st.session_state[f"period_{index}"] = False
    st.session_state._period_change_notice = index + 1


def _clear_chat_only():
    _workspace().clear_conversation(preserve_conditions=True)
    st.session_state.agent_messages = []
    st.session_state.pop("last_agent_reply", None)
    st.session_state.pop("sensitivity_result", None)


def _reset_all():
    _workspace().clear_conversation(preserve_conditions=False)
    st.session_state.agent_messages = []
    st.session_state.pop("last_agent_reply", None)
    st.session_state.pop("sensitivity_result", None)
    for key in ['pdf_cache', 'settlement_pdf', 'settlement_transfer']:
        st.session_state.pop(key, None)
    st.session_state.pop("future_pair_shared_costs", None)
    st.session_state.pop("future_pair_applied_shared_costs", None)
    st.session_state.pop("future_pair_separate_costs", None)
    st.session_state.pop("future_pair_price_drafts", None)
    for key in ("future_pair_missing", "future_pair_history_open", "future_pair_costs_open"):
        st.session_state.pop(key, None)
    for key in list(st.session_state):
        if key == "future_pair_price_b" or key.startswith("future_pair_filter_"):
            del st.session_state[key]
    # 새 비교는 직접 입력한 과거 경락가격의 화면 조건도 초기화합니다.
    for key in ("price_history_variety", "price_history_grade", "price_history_size",
                "price_history_package", "price_history_package_kg", "price_history_market_code",
                "price_history_trade_type", "price_history_interval_days", "price_history_base_date",
                "price_history_limit", "price_history_submitted_query", "show_price_history"):
        st.session_state.pop(key, None)
    _sync_card(force=True)


def _clear_sensitivity():
    st.session_state.pop("sensitivity_result", None)
    ws = _workspace()
    ws.entries = [] if any(x["tool"] in {"build_review_brief", "explain_decision_conditions"} for x in ws.entries) else [x for x in ws.entries if x["tool"] != "compare_sensitivity"]


def _clear_review():
    _workspace().entries = []
    st.session_state.pop("sensitivity_result", None)


def _render_cases(ws, disabled):
    from agents.juneok.casefiles import parse_case
    with st.expander("비교 건 저장·불러오기"):
        st.caption("조건과 당시 결과를 로컬 JSON으로 보관합니다. 대화·API 키는 포함하지 않습니다. 가져온 값의 사실 여부는 별도로 확인하세요.")
        name = st.text_input("저장할 비교 이름", value="내 출하 비교", max_chars=80, key="case_name")
        download_slot = st.empty()
        uploaded = st.file_uploader("저장한 비교 JSON", type=["json"], key="case_upload")
        if st.button("비교 파일 확인", disabled=uploaded is None or disabled):
            try:
                raw = uploaded.getvalue()
                saved = parse_case(raw)
                from agents.juneok.workspace import data_revision
                st.session_state.case_preview = {"digest": hashlib.sha256(raw).hexdigest(), "version": ws.store.version,
                    "name": saved["name"], "created": saved["created_at_kst"], "data_changed": saved["data_revision"] != list(data_revision()),
                    "archive_count": len(saved["archived_results"])}
            except ValueError as exc:
                st.session_state.pop("case_preview", None)
                st.error(str(exc))
        preview = st.session_state.get("case_preview")
        if preview and uploaded is not None and hashlib.sha256(uploaded.getvalue()).hexdigest() == preview["digest"]:
            st.write({"비교 이름": preview["name"], "저장 시각": preview["created"], "자료 버전 변경": preview["data_changed"], "당시 보관 결과 수": preview["archive_count"]})
            st.caption("복원하면 현재 조건을 대체하고 대화를 비웁니다. 당시 결과는 보관하고 현재 조건은 다시 계산합니다. 해시는 내용 변경 검사이며 작성자 인증이 아닙니다.")
            if st.button("현재 조건과 변경 사항 검토 · 복원하지 않음", disabled=disabled):
                compared = ws.compare_case(uploaded.getvalue(), preview["version"])
                if compared["status"] != "OK": st.error(compared["message"])
            if st.button("확인한 조건 복원 · 현재 기준 재계산", disabled=disabled):
                result = ws.restore_case(uploaded.getvalue(), preview["version"])
                if result["status"] == "OK":
                    st.session_state.agent_messages = []
                    for key in ["last_agent_reply", "sensitivity_result", "pdf_cache", "case_preview"]:
                        st.session_state.pop(key, None)
                    _sync_card(force=True)
                    st.success(result["message"])
                else:
                    st.error(result["message"])
    return download_slot, name


def _render_case_download(ws, name, disabled):
    if not disabled and name.strip():
        try:
            st.download_button("비교 JSON 내려받기", ws.export_case(name), file_name="감귤_비교기록.json", mime="application/json", key="download_case")
        except ValueError as exc:
            st.warning(str(exc))


def _discovery_changed(source, index, fields):
    for field in fields[index + 1:]:
        st.session_state.pop(f"discovery_{source}_{field}", None)
    st.session_state.pop("discovery_result", None)
    st.session_state.pop("discovery_candidate", None)
    _workspace().entries = []


@st.cache_data(show_spinner=False, max_entries=64)
def _discovery_choices(source, filters, revision):
    from agents.juneok.catalog import explore_data
    return explore_data(source, filters)


def _render_discovery(ws, disabled):
    from agents.juneok.catalog import explore_data, MARKET, SHIPMENT
    with st.expander("확보 자료 탐색·선택 · 키 없이 사용"):
        for entry in ws.current_entries():
            result = entry["result"]
            if entry["tool"] != "explore_data" or result.get("status") not in {"OK", "NO_DATA"} or result.get("ui_context", {}).get("channel") != "대화와 공유 조건": continue
            signature = json.dumps([result.get("filters"), result.get("catalog_source"), result.get("offset"), result.get("ui_context", {}).get("task_context_version"), list(ws.data_revision)], sort_keys=True)
            if st.session_state.get("discovery_chat_sync") != signature:
                src = result["catalog_source"]
                st.session_state.discovery_source = src
                for field in (MARKET if src == "market" else SHIPMENT):
                    st.session_state[f"discovery_{src}_{field}"] = result["filters"].get(field)
                st.session_state.discovery_page = result["offset"] // 20 + 1
                st.session_state.pop("discovery_candidate", None)
                st.session_state.discovery_result = {"result": result, "revision": ws.data_revision, "source": src, "filters": result["filters"], "page": st.session_state.discovery_page}
                st.session_state.discovery_chat_sync = signature
        source = st.selectbox("탐색할 자료", ["market", "shipment"], format_func=lambda x: {"market": "경매 거래규격", "shipment": "과거 출하 품종·연월"}[x], key="discovery_source", on_change=_clear_review)
        fields = MARKET if source == "market" else SHIPMENT
        filters = {}
        for i, field in enumerate(fields):
            choices = _discovery_choices(source, filters, ws.data_revision).get("available_filters", {}).get(field, [])
            key = f"discovery_{source}_{field}"
            if st.session_state.get(key) not in [None, *choices]: st.session_state.pop(key, None)
            value = st.selectbox("탐색 " + {"shipment_variety": "출하 품종", "year": "연도", "target_month": "월"}.get(field, FIELD_NAMES.get(field, field)),
                [None, *choices], format_func=lambda x: "선택 안 함" if x is None else str(x), key=key,
                on_change=_discovery_changed, args=(source, i, fields))
            if value is not None: filters[field] = value
        page = st.number_input("탐색 페이지 · 페이지당20개", min_value=1, value=1, step=1, key="discovery_page", on_change=_clear_review)
        if st.button("실제 자료 후보 탐색", disabled=disabled):
            st.session_state.pop("discovery_candidate", None)
            result = ws.explore(source, filters, (page - 1) * 20)
            st.session_state.discovery_result = {"result": result, "revision": ws.data_revision, "source": source, "filters": filters, "page": page}
        saved = st.session_state.get("discovery_result")
        if saved and saved["source"] == source and saved["filters"] == filters and saved["page"] == page and saved["revision"] == ws.data_revision:
            result = saved["result"]
            candidates = result.get("candidates", [])
            if candidates:
                chosen = st.selectbox("조회할 실제 후보", [None, *range(len(candidates))], format_func=lambda x: "후보를 직접 선택하세요" if x is None else " / ".join(str(v) for v in candidates[x]["conditions"].values()), key="discovery_candidate")
                if st.button("선택 후보의 관측자료 조회", disabled=disabled or chosen is None):
                    item = candidates[chosen]
                    if item.get("ambiguous_codes"):
                        st.warning("같은 표시명에 여러 원천 코드가 있습니다. 자동 합산하지 않으므로 관리자 확인이 필요합니다.")
                    elif source == "market": ws.market({**item["conditions"], "as_of": None})
                    else:
                        c = item["conditions"]
                        ws.history({"shipment_variety": c["shipment_variety"], "target_month": c["target_month"], "start_year": c["year"], "end_year": c["year"]})
            elif result.get("status") == "OK": st.info("이 페이지에 후보가 없습니다. 앞 페이지를 선택하세요.")
        st.caption("실제 조합만 제시합니다. 출하/경매 품종 자동 연결과 제시가격 자동 대입은 하지 않습니다.")


def _render_offer_limits(ws, disabled):
    with st.expander("제안가격에서 허용 감모·보관비 확인"):
        options = ws.store.scenario.get("options", [])
        if not options:
            st.info("비교할 물량·보관 대안과 고정 조건을 먼저 입력하세요.")
            return
        option = st.selectbox("허용 조건을 검토할 대안", [o["id"] for o in options], key="limits_option", on_change=_clear_review)
        quote = st.text_input("제안 단가 또는 사용자 가정(원/kg)", key="limits_quote", on_change=_clear_review)
        target = st.selectbox("역산할 항목 · 한 번에 하나", ["loss_percent", "daily_storage_cost"], format_func=lambda x: "기간 전체 감모율 상한" if x == "loss_percent" else "전체 물량 하루 보관비 상한", key="limits_target", on_change=_clear_review)
        if st.button("제안 단가의 허용 조건 계산", disabled=disabled): ws.offer_limits(option, quote.strip() or None, target)
        st.caption("고정할 다른 조건이 필요합니다. 역산 결과로 현재 조건을 자동 수정하지 않습니다.")


def _render_review(ws, disabled):
    with st.expander("자료와 조건을 함께 검토하기"):
        st.caption("사용자가 선택한 과거자료와 현재 비교 조건을 한 보고서로 묶습니다. 품종을 자동 연결하거나 경락가격을 제시가격으로 대입하지 않습니다.")
        variety = st.selectbox("브리핑 출하통계 품종", ["선택 안 함", "노지온주", "월동온주", "하우스온주", "한라봉", "천혜향", "레드향"], key="review_variety", on_change=_clear_review)
        month = st.number_input("브리핑 조회 월", min_value=1, max_value=12, value=11, key="review_month", on_change=_clear_review)
        first = st.number_input("브리핑 시작 연도", min_value=2018, max_value=2100, value=2018, key="review_first", on_change=_clear_review)
        last = st.number_input("브리핑 마지막 연도", min_value=2018, max_value=2100, value=2024, key="review_last", on_change=_clear_review)
        market = None
        if st.checkbox("동일 규격 경매 참고자료도 확인", key="review_market_enabled", on_change=_clear_review):
            fields = [("market_code", "시장코드"), ("variety", "경매 품종"), ("grade", "등급"), ("size", "크기"), ("package", "포장 종류")]
            market = {key: st.text_input("브리핑 " + label, key="review_" + key, on_change=_clear_review).strip() or None for key, label in fields}
            market["package_kg"] = st.number_input("브리핑 포장중량(kg)", min_value=0.01, value=None, key="review_package_kg", on_change=_clear_review)
            market["as_of"] = st.text_input("브리핑 기준일(YYYY-MM-DD, 선택)", key="review_as_of", on_change=_clear_review).strip() or None
        options = ws.store.scenario.get("options", [])
        option_id = st.selectbox("브리핑에서 설명할 대안", [o["id"] for o in options], key="review_option", on_change=_clear_review) if options else None
        include_grid = st.checkbox("직접 입력한 가정 범위도 함께 검토", key="review_include_grid", on_change=_clear_review)
        losses = st.session_state.get("sensitivity_losses", "")
        prices = st.session_state.get("sensitivity_prices", "")
        if include_grid:
            st.caption(f"가정 범위 입력란의 현재 값: 감모 {losses or '미입력'} / 미래 단가 {prices or '미입력'}. 값을 자동으로 만들지 않습니다.")
        if st.button("출하 검토 브리핑 만들기", disabled=disabled):
            query = {"shipment_variety": variety, "target_month": int(month), "start_year": int(first), "end_year": int(last)} if variety != "선택 안 함" else None
            ws.review(shipment_query=query, market_query=market, option_id=option_id,
                loss_values=[x.strip() for x in losses.split(",") if x.strip()] if include_grid else None,
                price_values=[x.strip() for x in prices.split(",") if x.strip()] if include_grid and prices.strip() else None)


def _render_task_context(ws):
    context = ws.session.task_context.snapshot()
    if context["goal"] is None:
        return
    with st.expander("이번 검토의 선택과 미확인 사항"):
        st.write("최근 수행 과업: " + context["goal"])
        st.caption("이번 비교에서 선택한 조건입니다. 사용자 장기 프로필이나 조회 결과의 최신성을 뜻하지 않습니다.")
        query = context["shipment_query"]
        if query:
            st.write({"출하통계 품종": query.get("shipment_variety"), "조회 월": query.get("target_month"),
                      "시작 연도": query.get("start_year"), "마지막 연도": query.get("end_year")})
        query = context["market_query"]
        if query:
            labels = {"market_code": "시장코드", "variety": "경매 품종", "grade": "등급", "size": "크기", "package": "포장", "package_kg": "포장중량(kg)", "as_of": "기준일"}
            st.write({labels[k]: v for k, v in query.items()})
        query = context["sensitivity_query"]
        if query and query.get("loss_values"):
            st.write({"기간 전체 감모 가정(%)": query["loss_values"], "미래 단가 가정(원/kg)": query.get("price_values")})
        from agents.juneok.reporting import _field
        pending = list(dict.fromkeys(x for items in context["pending"].values() for x in items))
        if pending:
            st.write("남은 확인: " + ", ".join(_field(x) for x in pending))
        else:
            st.caption("도구가 요청한 필수 입력의 미확인 항목은 없습니다. 저장·계약 등 실행 가능성은 결과에서 별도로 확인하세요.")


def _clear_csv_preview():
    st.session_state.pop("csv_preview", None)


def _render_csv_manager():
    from agents.juneok.csv_import import preview_auction_csv, apply_auction_preview
    with st.expander("자료 관리자 · 경매 CSV 추가"):
        st.caption("일반 사용자는 파일을 추가하지 않고 준비된 자료로 시작할 수 있습니다. #855 출하통계는 기존 자료를 유지합니다.")
        st.write("지원: 배포 경매 원본과 같은 36개 컬럼의 CSV, UTF-8/CP949, 최대 20MiB. 가공 가격표와 임의 CSV는 지원하지 않습니다.")
        st.caption("기존 경매 원본에 추가해 다시 집계합니다. 완전히 동일한 거래만 중복 제외합니다. 최신 관측이 없는 파일은 적용하지 않습니다. 이 설정은 이 설치본의 모든 사용자에게 적용됩니다.")
        uploaded = st.file_uploader("경매 원본 CSV 선택", type=["csv"], max_upload_size=20,
                                    key="market_csv_upload", on_change=_clear_csv_preview)
        if st.button("CSV 검사 · 변경 내용 미리보기", disabled=uploaded is None):
            _clear_csv_preview()
            try:
                with st.spinner("CSV 형식과 유효 거래를 검사합니다"):
                    st.session_state.csv_preview = preview_auction_csv(uploaded.getvalue())
            except ValueError as exc:
                st.error(str(exc))
            except (OSError, KeyError, TypeError, csv.Error):
                st.error("파일을 검사하지 못했습니다. 파일 형식과 저장 권한을 확인하세요. 현재 자료는 유지합니다.")
        preview = st.session_state.get("csv_preview")
        if preview:
            st.write(f"현재 마지막 관측 {preview['current']['observed_last_date']} → 적용 후 {preview['observed_last_date']}")
            st.write(f"선택 파일: 전체 {preview['input_rows']:,}건 · 유효 {preview['accepted_rows']:,}건 · 제외 {preview['rejected_rows']:,}건")
            st.caption(f"적용 후 범위 {preview['observed_first_date']} ~ {preview['observed_last_date']} · 단위 원/kg · 품종: {', '.join(preview['varieties'])}")
            st.caption(f"적용할 버전: {preview['snapshot_id']}")
            if preview["rejected_rows"]:
                st.warning("일부 행을 제외했습니다. 아래 사유를 확인한 뒤 적용하세요.")
                st.json(preview["rejected_by_primary_reason"])
            st.caption("검사만으로 자료는 바뀌지 않습니다. 적용하면 이전 가격 조회가 섞이지 않도록 대화를 초기화합니다. 공유 비교 조건은 유지합니다.")
            if st.button("확인한 자료 적용 · 대화 초기화"):
                try:
                    apply_auction_preview(preview)
                except ValueError as exc:
                    st.error(str(exc))
                except (OSError, KeyError, TypeError):
                    st.error("자료를 적용하지 못했습니다. 저장 권한과 파일 상태를 확인하세요.")
                else:
                    _clear_chat_only()
                    _clear_csv_preview()
                    st.session_state.csv_applied_notice = "경매 자료를 적용했습니다. 새 대화는 변경한 자료를 조회합니다."
                    st.rerun()



def _show_agent_reply(reply, current_version, draft_pending=False, past_future_pair=False):
    from agents.juneok.settlement_reporting import render_settlement, citation_details
    settlements = [x['result'] for x in reply.get('visible_results', []) if x['result'].get('analysis_type') == 'settlement' and 'citations' in x['result']]
    display_text = reply.get("text", "응답을 확인할 수 없습니다.")
    for result in settlements:
        display_text = display_text.replace(render_settlement(result), render_settlement(result, details=False))
    version = reply.get("state", {}).get("version")
    if past_future_pair:
        with st.expander("이전 두 출하일 비교 응답"):
            st.markdown(display_text)
    elif draft_pending or (version != current_version and not any(
            entry["tool"] == "compare_future_pair" for entry in reply.get("visible_results", []))):
        suffix = "카드에 저장 전 수정이 있습니다" if draft_pending else f"현재 버전 {current_version}"
        with st.expander(f"이전 조건 버전 {version}의 응답 · {suffix}"):
            st.markdown(display_text)
    else:
        st.markdown(display_text)
    for result in settlements:
        with st.expander(f"정산 근거 보기 · 기록 버전 {result['record_revision']}"):
            st.markdown(citation_details(result))


_SHIPMENT_PROGRESS = {
    "conditions": ("출하 조건 확인 중…", "알려주신 출하 조건과 더 필요한 정보를 확인합니다."),
    "price_history": ("과거 경락가격 CSV 조회 중…", "같은 거래규격의 경락가격을 로컬 CSV에서 확인합니다."),
    "market_csv": ("시장 가격 CSV 조회 중…", "저장된 경매 API 수집본(CSV)에서 시장 가격을 확인합니다."),
    "shipment_csv": ("출하 통계 CSV 조회 중…", "로컬 CSV에서 품종과 기간에 해당하는 출하 통계를 찾아봅니다."),
    "catalog_csv": ("확보 자료 살펴보는 중…", "로컬 CSV에서 조회할 수 있는 거래규격이나 출하 통계를 찾아봅니다."),
    "response": ("확인한 자료로 답변 준비 중…", "도구에서 확인한 내용을 바탕으로 답변을 준비합니다."),
    "compare": ("감모율과 비용을 반영해 비교 중…", "입력하신 감모율과 비용을 반영해 출하 금액을 비교합니다."),
}
_SHIPMENT_STEP_DETAILS = {
    "출하 조건 확인": _SHIPMENT_PROGRESS["conditions"][1],
    "과거 가격 조회": _SHIPMENT_PROGRESS["price_history"][1],
    "도구 결과 확인": _SHIPMENT_PROGRESS["response"][1],
    "감모율과 비용을 반영해 출하 비교": _SHIPMENT_PROGRESS["compare"][1],
}


def _shipment_progress_state(status):
    return "complete" if status in {None, "OK", "NEEDS_INPUT", "NO_DATA"} else "error"


def _shipment_work_label(elapsed):
    if elapsed is None:
        return "작업 과정"
    if elapsed < 1:
        return "1초 미만 작업함"
    minutes, seconds = divmod(int(elapsed), 60)
    duration = f"{minutes}분 {seconds}초" if minutes else f"{seconds}초"
    return f"{duration} 동안 작업함"


def _run_with_shipment_progress(run, started=None):
    started = monotonic() if started is None else started
    steps = []
    status = st.status(_SHIPMENT_PROGRESS["conditions"][0], type="compact", expanded=True)
    timer = status.empty()
    timer.html(_shipment_timer_html(started), unsafe_allow_javascript=True)

    def report(stage, *, initial=False):
        label, detail = _SHIPMENT_PROGRESS[stage]
        if not steps or steps[-1] != detail:
            steps.append(detail)
            if not initial:
                status.update(label=label)
            status.write(detail)

    report("conditions", initial=True)
    result = run(report)
    elapsed = monotonic() - started
    status.update(label=_shipment_work_label(elapsed), state=_shipment_progress_state(result["status"]), expanded=False)
    timer.empty()
    return result, steps, elapsed


def _shipment_timer_html(started):
    # This only inserts a server-measured numeric offset. The browser ticks without a rerun.
    timer_id = f"shipment-clock-{int(started * 1_000_000)}"
    elapsed_ms = max(0, int((monotonic() - started) * 1000))
    return f"""<span id="{timer_id}" hidden></span><script>
    (() => {{
        const marker = document.getElementById('{timer_id}');
        const summary = marker?.closest('details')?.querySelector('summary');
        if (!summary) return;
        const clock = document.createElement('span');
        clock.className = 'shipment-clock';
        clock.style.cssText = 'white-space:nowrap;font-variant-numeric:tabular-nums';
        clock.setAttribute('aria-hidden', 'true');
        const begin = performance.now() - {elapsed_ms};
        const tick = () => {{
            if (!marker.isConnected || !summary.isConnected) {{
                clock.remove();
                return false;
            }}
            clock.textContent = ` · ${{Math.floor((performance.now() - begin) / 1000)}}초`;
            if (!clock.isConnected) summary.firstElementChild?.append(clock);
            return true;
        }};
        if (tick()) {{
            const interval = setInterval(() => {{ if (!tick()) clearInterval(interval); }}, 200);
        }}
    }})();
    </script>"""


def _apply_future_pair_chat_edit(ws, messages, version, patch, user_text):
    with st.chat_message("assistant"):
        result, steps, elapsed = _run_with_shipment_progress(
            lambda report: ws.session.future_pair.apply("edit", version, patch, on_progress=report)
        )
    if result["status"] == "INVALID_INPUT":
        st.warning(result["message"])
        return
    entry = {"tool": "compare_future_pair", "result": result}
    reply = {"state": ws.store.snapshot(), "text": render_dialogue([entry]), "visible_results": [entry],
             "progress_steps": steps, "progress_elapsed": elapsed, "status": result["status"]}
    reply["chat_report"] = _chat_report(ws, reply)
    messages.extend([
        {"role": "user", "content": user_text},
        {"role": "assistant", "reply": reply},
    ])
    st.rerun()


def _render_later_shipment_edit(ws, messages, pair, version):
    start = date.fromisoformat(pair["start_date"])
    earlier = date.fromisoformat(pair["option_a"]["sale_date"])
    later = date.fromisoformat(pair["options_b"][0]["sale_date"])
    last_day = start + timedelta(days=min(365, (date.max - start).days))
    with st.expander("나중 출하일 바꾸기"):
        st.caption(f"먼저 출하하는 {earlier}과 나머지 조건을 유지합니다. 새 보관기간의 감모율을 이어서 확인합니다.")
        with st.form(f"future_pair_chat_date_{version}"):
            changed = st.date_input("새 나중 출하일", value=later,
                                    min_value=earlier + timedelta(days=1), max_value=last_day)
            submitted = st.form_submit_button("이 날짜로 변경")
        if submitted:
            if changed is None or changed == later:
                st.info("현재와 다른 출하일을 골라주세요.")
            else:
                days = (changed - start).days
                _apply_future_pair_chat_edit(ws, messages, version, {"option_b": {"delay_days": days}},
                                             f"나중 출하일만 {changed}로 변경 · {days}일 보관")


def _submit_shipment_question(example=None):
    question = example if example is not None else st.session_state.get("shipment_question", "")
    messages = st.session_state.setdefault("agent_messages", [])
    if question.strip() and not (messages and messages[-1].get("pending")):
        messages.append({"role": "user", "content": question.strip(), "pending": True, "started": monotonic()})


def _render_shipment_composer(disabled, show_examples, *, busy=False):
    if show_examples:
        examples = (
            ("지금 팔지 더 보관할지 비교", "감귤을 지금 팔 때와 더 보관하고 팔 때를 비교해 주세요."),
            ("두 출하일 비교 시작", "두 미래 출하일에 감귤을 팔 때를 비교해 주세요."),
        )
        with st.container(key="shipment_examples", horizontal=True, gap="small"):
            for label, example in examples:
                st.button(label, disabled=disabled, on_click=_submit_shipment_question, args=(example,))
    suffix = "_busy" if busy else ""
    with st.container(key=f"shipment_composer{suffix}"):
        with st.form(f"shipment_chat{suffix}", clear_on_submit=True, border=False):
            field, action = st.columns([5, 1], vertical_alignment="bottom")
            field.text_input(
                "출하 고민을 입력하세요",
                key=f"shipment_question{suffix}",
                placeholder="예: 감귤 3톤, 언제 팔까요?",
                disabled=disabled,
            )
            action.form_submit_button(
                "보내기", key=f"shipment_send{suffix}", type="primary", disabled=disabled, width="stretch",
                on_click=_submit_shipment_question,
            )


def _render_chat_pair_actions(ws, messages, pair, current_pair, latest_message):
    if pair["status"] == "OK" and current_pair is not None:
        _render_later_shipment_edit(ws, messages, current_pair, pair["future_pair_version"])
    elif pair.get("manual_price_required") and latest_message:
        with st.form(f"future_pair_chat_manual_{pair['future_pair_version']}"):
            price = st.number_input("나중 출하의 가정 단가 (원/kg)", min_value=0.01, value=None)
            submitted = st.form_submit_button("이 단가로 비교")
        if submitted:
            if price is None:
                st.warning("나중 출하의 가정 단가를 입력해 주세요.")
            else:
                _apply_future_pair_chat_edit(ws, messages, pair["future_pair_version"],
                                             {"price_mode": "manual", "price_b": price},
                                             f"나중 출하 단가를 kg당 {price:,.12g}원으로 비교")


def _chat_pair(reply):
    return next((entry["result"] for entry in reply.get("visible_results", [])
                 if entry["tool"] == "compare_future_pair"), None)


def _chat_report(ws, reply):
    pair = _chat_pair(reply)
    if pair is not None:
        state = ws.session.future_pair
        result = state.current_result()
        if pair["status"] != "OK" or result is None or pair["future_pair_version"] != state.version:
            return None
        return {
            "kind": "future_pair", "version": pair["future_pair_version"],
            "entries": [{"tool": "compare_future_pair", "result": result}],
            "json": json.dumps({"format": "jeju-future-pair", "version": 1, "result": result},
                               ensure_ascii=False, indent=2),
        }
    if not any(item["tool"] == "compare_shipment_options" and item["result"]["status"] == "OK"
               for item in reply.get("visible_results", [])):
        return None
    entries = ws.current_entries() or reply["visible_results"]
    try:
        report_json = ws.export_case("내 출하 비교")
    except ValueError:
        report_json = ""
    return {"kind": "comparison", "version": reply.get("state", {}).get("version", ws.store.version),
            "entries": entries, "json": report_json}


def _latest_chat_report(messages, version, current_pair):
    for index in range(len(messages) - 1, -1, -1):
        reply = messages[index].get("reply", {})
        if report := reply.get("chat_report"):
            if report["kind"] == "future_pair":
                current = current_pair == report["entries"][0]["result"]
            else:
                current = version == report["version"]
            return (index, report) if current else (None, None)
        if any(item["tool"] in {"compare_shipment_options", "compare_future_pair"} and
               item["result"].get("status") in {"OK", "NEEDS_INPUT", "NEEDS_CLARIFICATION", "NO_DATA"}
               for item in reply.get("visible_results", [])):
            break
    return None, None


def _render_chat_report(report):
    if report["kind"] == "future_pair":
        from agents.juneok.future_pair_ui import render_future_pair_result
        render_future_pair_result(report["entries"][0]["result"], "chat", report_json=report["json"])
    else:
        _render_report(report["entries"], chat_json=report["json"])


def _render_shipment_chat(ws, key_present, draft_pending, settlement_dirty, current_pair):
    if not key_present:
        st.title("감귤, 언제 출하할까요?")
        st.info("채팅을 사용하려면 연결 설정이 필요합니다. 위의 ‘직접 입력’에서 출하를 비교할 수 있습니다.")
        _render_shipment_composer(disabled=True, show_examples=False)
        return
    if draft_pending or settlement_dirty:
        st.warning("직접 입력에서 수정 중인 내용이 있습니다. 적용하거나 취소한 뒤 대화를 이어가세요.")
    messages = st.session_state.setdefault("agent_messages", [])
    pending = messages[-1] if messages and messages[-1].get("pending") else None
    disabled = draft_pending or settlement_dirty or pending is not None
    if not messages:
        st.title("감귤, 언제 출하할까요?")

    pair_state = ws.session.future_pair
    latest_pair_message = next((index for index in reversed(range(len(messages)))
                                if any(entry["tool"] == "compare_future_pair"
                                       for entry in messages[index].get("reply", {}).get("visible_results", []))), None)
    previous_pair_actions = None
    previous_report_slot = None
    report_index, previous_report = _latest_chat_report(messages, ws.store.version, current_pair)
    transcript = st.container(key="shipment_transcript", height=640, border=False, autoscroll=True) if messages else None
    for index, message in enumerate(messages):
        with transcript.chat_message(message["role"]):
            reply = message.get("reply")
            if not reply:
                st.markdown(message["content"])
                continue
            pair = _chat_pair(reply)
            past_pair = pair is not None and (
                pair.get("future_pair_version") != pair_state.version
                or (pair["status"] == "OK" and current_pair is None)
            )
            if steps := reply.get("progress_steps"):
                with st.status(_shipment_work_label(reply.get("progress_elapsed")),
                               state=_shipment_progress_state(reply.get("status")), type="compact"):
                    for step in steps:
                        st.write(_SHIPMENT_STEP_DETAILS.get(step, step))
            _show_agent_reply(reply, ws.store.version, draft_pending, past_pair)
            if index == report_index and not (draft_pending or settlement_dirty):
                if pending is None:
                    _render_chat_report(previous_report)
                else:
                    previous_report_slot = st.empty()
            if pair is None or past_pair or index != latest_pair_message or draft_pending or settlement_dirty:
                continue
            if pending is not None:
                if pair["status"] == "OK":
                    previous_pair_actions = (st.empty(), pair)
                continue
            _render_chat_pair_actions(ws, messages, pair, current_pair, index == len(messages) - 1)

    composer_slot = getattr(st, "bottom", None) if messages else None
    with composer_slot if composer_slot is not None else st.container(key="shipment_welcome_input"):
        composer = st.empty()
    if pending is not None:
        with composer.container():
            _render_shipment_composer(disabled=True, show_examples=False, busy=True)
        with transcript.chat_message("assistant"):
            pending.pop("pending")
            started = pending.pop("started", None)
            if _REPORT_REQUEST.fullmatch(pending["content"]):
                message = "방금 비교한 결과의 보고서입니다." if previous_report else "내려받을 최근 출하 비교 결과가 없습니다. 출하 조건을 비교한 뒤 다시 요청해 주세요."
                reply = {"status": "OK", "text": message, "state": ws.store.snapshot(), "chat_report": previous_report}
                steps, elapsed = [], None
            else:
                reply, steps, elapsed = _run_with_shipment_progress(
                    lambda report: ws.run(pending["content"], on_progress=report), started
                )
                reply["chat_report"] = _chat_report(ws, reply)
                st.session_state.last_agent_reply = {"status": reply.get("status"), "metrics": reply.get("metrics", {})}
            messages.append({"role": "assistant", "reply": {**reply, "progress_steps": steps,
                                                            "progress_elapsed": elapsed}})
            _show_agent_reply(reply, ws.store.version)
            if report := reply.get("chat_report"):
                _render_chat_report(report)
            pair = _chat_pair(reply)
            if pair is not None and pair.get("future_pair_version") == pair_state.version:
                _render_chat_pair_actions(ws, messages, pair, pair_state.current_result(), True)
        if pair is None and previous_pair_actions is not None:
            slot, previous_pair = previous_pair_actions
            if previous_pair.get("future_pair_version") == pair_state.version:
                with slot.container():
                    _render_chat_pair_actions(ws, messages, previous_pair, pair_state.current_result(), False)
        active_report_index, _ = _latest_chat_report(messages, ws.store.version, pair_state.current_result())
        if previous_report_slot is not None and active_report_index == report_index:
            with previous_report_slot.container():
                _render_chat_report(previous_report)
    with composer.container():
        _render_shipment_composer(draft_pending or settlement_dirty, show_examples=not messages)


def _render_report(entries, *, chat_json=None):
    if not entries:
        return
    markdown = render_report(entries)
    # Cache bytes by the frozen report, not by mutable widget state.
    stamp = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    cached = st.session_state.get("pdf_cache")
    if not cached or cached[0] != stamp:
        try:
            from agents.juneok.pdf_report import render_pdf_report
            cached = (stamp, render_pdf_report(entries))
            st.session_state.pdf_cache = cached
        except Exception:
            cached = None
    if chat_json is not None:
        with st.container(border=True):
            st.markdown("**감귤 출하 비교 보고서**")
            if cached:
                st.download_button("PDF 보고서 내려받기", cached[1], "제주감귤_비교보고서.pdf", "application/pdf",
                                   key="download_chat_pdf", on_click="ignore")
            else:
                st.warning("PDF를 만들지 못했습니다. 보고서 미리보기와 JSON은 확인할 수 있습니다.")
            with st.expander("보고서 미리보기"):
                st.markdown(markdown)
            with st.expander("상세 · JSON"):
                if chat_json:
                    st.download_button("비교 JSON 내려받기", chat_json, "감귤_비교기록.json", "application/json",
                                       key="download_chat_json", on_click="ignore")
                else:
                    st.caption("현재 공유 조건을 JSON으로 저장할 수 없습니다.")
        return
    st.subheader("결과와 보고서 미리보기")
    st.markdown(markdown)
    if cached:
        st.download_button("PDF 보고서 내려받기", cached[1], file_name="제주감귤_비교보고서.pdf", mime="application/pdf", key="download_pdf")
    else:
        st.error("PDF를 만들지 못했습니다. 미리보기와 입력 조건은 보존했습니다. 설치와 한글 글꼴 파일을 확인한 뒤 다시 시도하세요.")
    with st.expander("보조 형식 · Markdown"):
        st.download_button("MD 내려받기", markdown, file_name="jeju-report.md", mime="text/markdown", key="download_md")


def _render_card_summary(ws):
    st.subheader("현재 조건 카드")
    st.caption(f"대화와 같은 조건 버전 {ws.store.version}을 사용합니다. 키 없이 직접 수정·계산할 수도 있습니다.")
    st.dataframe(condition_rows(ws.store.scenario, ws.input_context), hide_index=True, width="stretch")
    st.button("가상 예시 조건 불러오기", on_click=_load_demo)
    st.button("편집 취소 · 현재 조건 불러오기", on_click=_reload_cards)


def _render_cards(ws):
    with st.expander("조건 직접 입력·수정", expanded=False):
        st.selectbox("이번 비교의 입력 구분", INPUT_CONTEXTS, key="input_context", help="사용자 표시이며 사실 검증이 아닙니다. 미래 단가는 사용자 가정입니다.")
        st.number_input("비교할 보관 대안 수", min_value=1, max_value=3, step=1, key="option_count")
        st.markdown("#### 1. 지금 판매할 조건")
        left, right = st.columns(2)
        left.text_input("물량(kg)", key="quantity", placeholder="모르면 빈칸")
        right.text_input("지금 판매 가능한 단가(원/kg)", key="price", placeholder="실제 제시가격 또는 명시적인 가정")
        st.selectbox("가격 기준", ["미확인", "수수료 차감 후 단가", "수수료 차감 전 단가"], key="basis")
        st.text_input("현재 수수료율(%) · 차감 전 단가일 때 입력", key="current_fee", placeholder="실제 0%인 경우에만 0")
        st.markdown("#### 2. 가격에 포함된 비용과 별도 비용")
        st.multiselect("단가에 이미 반영된 비용", list(COST_LABELS), key="included")
        st.checkbox("단가에 포함된 비용을 확인했습니다. 선택하지 않은 항목은 별도 처리 기준을 확인합니다.", key="included_confirmed")
        st.selectbox("보관비 외 기타 고정비용 처리", ["미확인", "보관비 외 기타 고정비용은 양쪽 동일하여 제외", "대안별 비용을 항목별 입력"], key="policy")
        with st.expander("현재 출하의 별도 비용 · 항목별 입력을 선택한 경우"):
            for label, category in COST_LABELS.items():
                if category != "commission":
                    st.text_input(f"현재 {label}(원)", key="current_" + category)
            st.checkbox("현재 출하에 별도 차감할 비용이 없습니다", key="current_none")
        st.markdown("#### 3. 보관 후 판매할 조건")
        st.caption("모르는 값은 빈칸으로 두세요. 감모는 보관기간 전체의 비율, 하루 보관비는 전체 물량의 비용입니다.")
        for i in range(st.session_state.option_count):
            st.markdown(f"**보관 대안 {i + 1}**")
            st.number_input(f"대안 {i + 1} 보관기간(일)", min_value=1, max_value=365, value=None, step=1, key=f"days_{i}", placeholder="실제 비교할 기간을 입력", on_change=_on_period_edit, args=(i,))
            columns = st.columns(2)
            columns[0].text_input(f"대안 {i + 1} 기간 전체 감모율(%)", key=f"loss_{i}", placeholder="모르면 빈칸 · 하루 감모율 아님")
            columns[1].text_input(f"대안 {i + 1} 하루 추가 보관비(원)", key=f"storage_{i}", placeholder="실제 무료라면 0")
            st.checkbox(f"대안 {i + 1} 감모율은 해당 보관기간 전체의 값으로 확인했습니다", key=f"period_{i}")
            st.text_input(f"대안 {i + 1} 미래 단가 가정(원/kg) · 선택 입력", key=f"future_{i}", placeholder="없으면 손익분기만 계산")
            st.text_input(f"대안 {i + 1} 수수료율(%) · 차감 전 단가일 때 입력", key=f"fee_{i}")
            with st.expander(f"대안 {i + 1} 별도 비용 · 항목별 입력을 선택한 경우"):
                for label, category in COST_LABELS.items():
                    if category != "commission":
                        st.text_input(f"대안 {i + 1} {label}(원)", key=f"option_{i}_{category}")
                st.checkbox(f"대안 {i + 1}에 일별 보관비 외 별도 차감 비용이 없습니다", key=f"option_{i}_none")
        st.markdown("#### 4. 실제로 실행할 수 있는 조건")
        st.caption("저장·출하 조건이 미확인이어도 가상 경제성은 비교할 수 있습니다. 계산이 된다는 사실이 실행 가능함을 뜻하지는 않습니다.")
        st.selectbox("저장 가능한 조건인가요?", ["미확인", "예", "아니오"], key="storage_available")
        st.selectbox("출하일을 변경할 수 있나요?", ["미확인", "예", "아니오"], key="shipping_flexible")
        submitted = st.button("입력 조건 비교", type="primary", disabled=st.session_state.card_base_version != ws.store.version)
    changed_period = st.session_state.get("_period_change_notice")
    if changed_period and not st.session_state.get(f"period_{changed_period - 1}"):
        st.info("보관기간이 변경되어 기간 전체 감모율 확인을 해제했습니다. 새 기간의 감모를 확인하세요.")
    dirty = _card_dirty()
    if st.session_state.card_base_version != ws.store.version:
        st.error("카드 편집 중 공유 조건이 바뀌었습니다. 현재 조건을 불러온 뒤 다시 수정하세요.")
    if submitted:
        if dirty:
            updated = ws.apply_card(dict(form_snapshot(st.session_state)), st.session_state.card_base_version, st.session_state.card_option_ids)
            if updated["status"] != "OK":
                st.error(updated.get("message", "조건을 저장하지 못했습니다."))
            else:
                st.session_state._force_card_sync = True
                st.session_state.pop("sensitivity_result", None)
                st.rerun()
        else:
            ws.calculate()
    return dirty


def _render_price_history(ws, disabled):
    with st.expander("과거 경락가격 비교 · 직접 조회(키 없이 사용)",
                     expanded=st.session_state.get("show_price_history", False)):
        st.caption("경매시장에서 관측한 같은 거래규격의 과거 가격쌍입니다. 농가 실수취가·미래가격·보관 수익의 예측이 아닙니다.")
        left, right = st.columns(2)
        with left:
            variety = st.text_input("경매 품종", key="price_history_variety", placeholder="예: 조생귤")
            grade = st.text_input("경매 등급", key="price_history_grade", placeholder="예: 특")
            package = st.text_input("경매 포장 종류", key="price_history_package", placeholder="예: 상자 (5kg는 별도 입력)")
            market_code = st.text_input("시장코드 · 선택", key="price_history_market_code", placeholder="예: 110001")
        with right:
            size = st.text_input("경매 크기", key="price_history_size", placeholder="예: 소")
            package_kg = st.text_input("포장 한 개의 중량(kg)", key="price_history_package_kg", placeholder="예: 5")
            trade_type = st.text_input("거래방식 · 선택", key="price_history_trade_type", placeholder="예: 경매")
            interval = st.number_input("과거 가격 비교 간격(일)", key="price_history_interval_days",
                                       min_value=1, max_value=366, value=None, step=1,
                                       help="현재 동결 비교 자료는 3·7·14일입니다. 다른 간격은 지원 여부를 안내합니다.")
        base_date = st.text_input("과거 기준일 · 선택 (YYYY-MM-DD)", key="price_history_base_date",
                                  placeholder="미지정이면 같은 조건의 모든 기준일")
        limit = st.number_input("표시할 과거 사례 수", key="price_history_limit", min_value=1,
                                 max_value=10, value=3, step=1)
        fields = {"variety": variety, "grade": grade, "size": size, "package": package,
                  "package_kg": package_kg, "market_code": market_code,
                  "trade_type": trade_type, "base_date": base_date}
        query = {key: value.strip() for key, value in fields.items() if value.strip()}
        if interval is not None:
            query["interval_days"] = int(interval)
        query["limit"] = int(limit)
        # 입력을 바꾼 직후 이전 결과를 새 조회값의 증거처럼 내보내지 않습니다.
        previous = st.session_state.get("price_history_submitted_query")
        if (previous is not None and previous != query and len(ws.entries) == 1
                and ws.entries[0].get("tool") == "get_price_history"
                and ws.entries[0].get("result", {}).get("ui_context", {}).get("channel") == "과거 경락가격 직접 조회"):
            ws.entries = []
            ws.result_version = None
            st.session_state.pop("pdf_cache", None)
            st.info("조회 조건이 바뀌어 이전 과거 가격 결과를 숨겼습니다. 다시 조회해 주세요.")
        if st.button("과거 경락가격 직접 조회", disabled=disabled):
            st.session_state.price_history_submitted_query = dict(query)
            result = ws.price_history(query)
            if result.get("status") == "DATA_ERROR":
                st.warning("가격 비교 자료의 원천 검증이 실패했습니다. 검증 전 가격 결과는 표시하지 않습니다.")
        st.caption("비교 간격은 과거 두 거래일 사이의 달력일이며, 출하 손익 계산의 보관일과 다릅니다. "
                   "시장·거래방식이 자료 전체에서 하나뿐이라 자동 적용되더라도 사용자 지정 조건으로 저장하지 않습니다. "
                   "수정한 입력은 '직접 조회'를 눌러야 후속 대화에 반영됩니다.")


def _render_history(ws, disabled):
    with st.expander("과거 출하통계 직접 조회 · 키 없이 사용", expanded=st.session_state.get("show_history", False)):
        st.caption("#855 과거 신고 출하량입니다. 현재 공급량·미래가격이 아닙니다. 경매 품종과 자동 매핑하지 않습니다.")
        variety = st.selectbox("출하 통계의 품종", ["노지온주", "월동온주", "하우스온주", "한라봉", "천혜향", "레드향"])
        month = st.number_input("조회 월", min_value=1, max_value=12, value=11)
        start = st.number_input("시작 연도", min_value=2018, max_value=2100, value=2018)
        end = st.number_input("마지막 연도", min_value=2018, max_value=2100, value=2024)
        if st.button("과거 출하량 조회", disabled=disabled):
            ws.history({"shipment_variety": variety, "target_month": int(month), "start_year": int(start), "end_year": int(end)})


def _render_sensitivity(ws, disabled):
    with st.expander("내 가정 범위로 비교하기"):
        options = ws.store.scenario.get("options", [])
        if not options:
            st.info("먼저 비교할 보관 대안과 비용을 입력하세요.")
            return
        st.caption("현재 공유 조건의 기간·비용을 고정하고 직접 정한 가정만 비교합니다. 저장된 감모와 미래 단가는 변경하지 않습니다.")
        option_id = st.selectbox("범위를 비교할 보관 대안", [o["id"] for o in options], key="sensitivity_option", on_change=_clear_sensitivity)
        losses = st.text_input("기간 전체 감모율 가정(%, 쉼표로 구분)", key="sensitivity_losses", on_change=_clear_sensitivity)
        prices = st.text_input("미래 단가 가정(원/kg, 선택)", key="sensitivity_prices", on_change=_clear_sensitivity)
        if st.button("가정 범위 비교", disabled=disabled):
            st.session_state.sensitivity_result = ws.sensitivity(option_id, [x.strip() for x in losses.split(",") if x.strip()],
                [x.strip() for x in prices.split(",") if x.strip()] if prices.strip() else None)
        if st.button("판단이 바뀌는 조건 설명", disabled=disabled):
            ws.explain_conditions(option_id, [x.strip() for x in losses.split(",") if x.strip()] if losses.strip() else None,
                [x.strip() for x in prices.split(",") if x.strip()] if prices.strip() else None)


def main():
    st.set_page_config(page_title="제주 감귤 출하 대안 비교", page_icon="🍊",
                       layout="wide", initial_sidebar_state="collapsed")
    st.html("""
        <style>
        .st-key-shipment_views, :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) {
            max-width: 48rem;
            margin-inline: auto;
        }
        .st-key-shipment_chat_area h1 {
            font-size: clamp(1.75rem, 4vw, 2.75rem);
            word-break: keep-all;
        }
        .st-key-shipment_transcript {
            max-height: max(10rem, calc(100dvh - 20rem));
        }
        .stApp:has([data-testid="stBottom"] .st-key-shipment_composer_busy) .st-key-shipment_welcome_input {
            display: none;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) [data-testid="stHorizontalBlock"] {
            display: grid;
            grid-template-columns: minmax(0, 1fr) max-content;
            align-items: end;
            gap: .375rem;
            padding: .25rem .375rem;
            border: 2px solid #9a5833;
            border-radius: 1rem;
        }
        .st-key-shipment_composer [data-testid="stHorizontalBlock"]:focus-within {
            border-color: #c85c2c;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) [data-testid="stColumn"] {
            width: auto;
            min-width: 0;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) :is([data-testid="stTextInputRootElement"], [data-baseweb="input"]) {
            border-color: transparent;
            min-height: 2.75rem;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) [data-testid="stForm"] {
            padding: 0;
        }
        :is(.st-key-shipment_composer, .st-key-shipment_composer_busy) button[kind="primaryFormSubmit"] {
            min-height: 2.75rem;
        }
        .st-key-shipment_composer button[kind="primaryFormSubmit"]:not(:disabled) {
            background: #aa451b;
            border-color: #aa451b;
            color: white;
        }
        .st-key-shipment_composer button[kind="primaryFormSubmit"]:hover:not(:disabled) {
            background: #8c3314;
        }
        .st-key-shipment_examples button {
            min-height: 2.25rem;
            border-radius: 1rem;
            padding-inline: .75rem;
            background: transparent;
        }
        .st-key-shipment_examples button:hover:not(:disabled) {
            border-color: #aa451b;
            background: rgba(170, 69, 27, .12);
            color: inherit;
        }
        .st-key-shipment_examples button:focus-visible {
            outline: 2px solid #c85c2c;
            outline-offset: 2px;
        }
        </style>
    """)
    settings = Settings.load()
    ws = _workspace()
    if ws.check_data_change():
        st.session_state.agent_messages = []
        st.session_state.pop("pdf_cache", None)
        st.session_state.pop("sensitivity_result", None)
        st.info("사용 자료가 바뀌어 이전 대화·결과를 비웠습니다. 비교 조건은 유지했습니다.")
    if ws.session.settings != settings:
        ws.session.settings = settings
        ws.session.client = None
        st.session_state.pop("last_agent_reply", None)
    _sync_card(force=st.session_state.pop("_force_card_sync", False))
    from agents.juneok.settlement_ui import hide_pending_settlement
    settlement_dirty = hide_pending_settlement(ws)
    with st.sidebar:
        st.subheader("실행과 자료")
        connection_slot = st.empty()
        st.caption("준비된 CSV로 시작합니다. 키 없는 조건 카드·통계·PDF도 사용할 수 있습니다.")
        try:
            from agents.juneok.data_refresh import market_status
            market = market_status()
            st.caption(f"가격자료 {market['snapshot_id']}: {market['observed_first_date']} ~ {market['observed_last_date']}. 실시간 시세가 아닙니다.")
            st.caption("#855 출하통계: 2018-01-01 ~ 2025-09-17. 현재 공급량이 아닙니다.")
        except (OSError, ValueError, KeyError, TypeError):
            st.warning("자료 목록을 읽지 못했습니다. 각 도구의 출처를 확인하세요.")
        st.button("설정 상태 새로 확인")
        st.button("대화만 비우기 · 조건 유지", on_click=_clear_chat_only)
        st.button("새 비교 시작 · 조건도 초기화", on_click=_reset_all)
        if notice := st.session_state.pop("csv_applied_notice", None):
            st.success(notice)
        _render_csv_manager()
        case_download_slot, case_name = _render_cases(ws, _card_dirty())
    with st.container(key="shipment_views"):
        chat, direct, tools = st.tabs(["대화", "직접 입력", "자료·도구"],
                                     key="shipment_view", on_change="rerun")
        with chat, st.container(key="shipment_chat_area"):
            chat_slot = st.empty()
            chat_report = st.empty()
        dirty_before = _card_dirty()
        messages = st.session_state.get("agent_messages", [])
        chat_running = bool(settings.api_key and messages and messages[-1].get("pending"))
        if not chat.open:
            st.html('<style>[data-testid="stBottom"]:has(:is(.st-key-shipment_composer, .st-key-shipment_composer_busy)) {display: none}</style>')
        if chat_running:
            # Run the turn before the condition widgets bind to the shared state.
            with chat_slot.container():
                _render_shipment_chat(ws, True, dirty_before, settlement_dirty, ws.session.future_pair.current_result())
            _sync_card()
            dirty_before = _card_dirty()
        with direct:
            current_tab, future_tab = st.tabs(["오늘과 나중", "두 미래 출하안 비교"])
            with current_tab:
                _render_card_summary(ws)
                if st.button("출하 조건 비교", disabled=dirty_before):
                    ws.calculate()
                dirty = _render_cards(ws)
                comparison_report = st.empty()
            with future_tab:
                from agents.juneok.future_pair_ui import render_future_pair
                future_result = render_future_pair(ws.session.future_pair)
        with tools:
            data_tab, tools_tab = st.tabs(["자료 조회", "추가 도구"])
            with data_tab:
                if st.button("과거 경락가격", disabled=dirty):
                    st.session_state.show_price_history = True
                if st.button("과거 출하자료", disabled=dirty):
                    st.session_state.show_history = True
                _render_price_history(ws, dirty)
                _render_history(ws, dirty)
                _render_discovery(ws, dirty)
                data_report = st.empty()
            with tools_tab:
                if st.button("용어 이해", disabled=dirty):
                    from agents.juneok.tools import explain_term
                    ws._freeze([{"tool": "explain_term", "result": explain_term("loss_rate")}], "용어 이해")
                _render_sensitivity(ws, dirty)
                _render_offer_limits(ws, dirty)
                _render_review(ws, dirty)
                from agents.juneok.settlement_ui import render_shipments
                render_shipments(ws, dirty)
                tools_report = st.empty()
                if not dirty:
                    _render_task_context(ws)
                with st.expander("사용 안내와 자료의 한계"):
                    st.markdown("모르는 값은 빈칸으로 두고, 비용이 없다고 확인한 경우에만 0을 입력하세요. 기간 전체 감모와 하루 비용을 구분하고, 단가에 포함된 비용은 한 번만 반영합니다. 실제 출하 전에 저장시설·계약·납기 조건을 확인하세요.")
                    st.caption("경매 관측가격, 농가 제시가격, 출하량, 시장 반입량은 각각 다른 자료입니다.")
    # Keep the composer mounted so switching tabs also preserves unsubmitted text.
    if not chat_running:
        with chat_slot.container():
            _render_shipment_chat(ws, bool(settings.api_key), dirty, settlement_dirty, future_result)
    connection_slot.write(connection_label(bool(settings.api_key), st.session_state.get("last_agent_reply")))
    if dirty:
        st.warning("입력 조건이 바뀌었습니다. ‘입력 조건 비교’를 눌러 적용하세요. 이전 결과·보고서·가정 범위 표는 숨겼습니다.")
        st.session_state.pop("sensitivity_result", None)
    if not dirty:
        entries = ws.current_entries()
        tool_names = {entry["tool"] for entry in entries}
        report_slot = comparison_report
        if any(entry["result"].get("ui_context", {}).get("channel") == "대화와 공유 조건" for entry in entries):
            report_slot = chat_report
        elif tool_names & {"compare_sensitivity", "solve_offer_limits", "build_review_brief",
                         "review_case_changes", "explain_decision_conditions", "explain_term"}:
            report_slot = tools_report
        elif not tool_names & {"compare_shipment_options"} and tool_names & {
            "get_price_history", "get_historical_shipment_context", "get_market_evidence", "explore_data"
        }:
            report_slot = data_report
        if report_slot is not chat_report or not any(entry["tool"] == "compare_shipment_options" and
                                                     entry["result"]["status"] == "OK" for entry in entries):
            with report_slot.container():
                _render_report(entries)
    # 직접 조회·손익 계산 이후 확정된 결과로 사이드바 JSON 다운로드를 생성합니다.
    with case_download_slot.container():
        _render_case_download(ws, case_name, dirty)


if __name__ == "__main__":
    main()
