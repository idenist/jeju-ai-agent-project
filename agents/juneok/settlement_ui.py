"""Keyless explicit confirmation flow; financial records never auto-enter chat."""
import json
from copy import deepcopy
import streamlit as st
from .settlement import empty_values, normalize, reconcile, parse_shipment, SCALARS, FLAGS, KINDS, CATEGORIES
from .settlement_reporting import render_settlement, citation_details


def _revoke(ws):
    ws.session.messages = []
    st.session_state.agent_messages = []
    st.session_state.pop("settlement_pdf", None)
    st.session_state.pop("pdf_cache", None)
    st.session_state.pop("settlement_transfer", None)
    # A copied financial reply in the comparison export would bypass record isolation.
    ws.entries = []


def hide_pending_settlement(ws):
    """Check widget draft before chat/report rendering, not after showing old money."""
    store = ws.shipment
    if not store.snapshot(): return False
    v = store.values()
    token = f"shipment_{store.snapshot()['record_id']}_{store.revision}"
    if token + '_kind' not in st.session_state: return False
    raw = deepcopy(v)
    raw['input_kind'] = st.session_state[token + '_kind']
    for field in SCALARS: raw[field] = st.session_state.get(token + field, v[field])
    for field in FLAGS: raw[field] = st.session_state.get(token + field, v[field])
    for group in ['sales', 'deductions', 'external_costs']:
        count = st.session_state.get(token + group, len(v[group]))
        raw[group] = []
        for i in range(count):
            item = deepcopy(v[group][i]) if i < len(v[group]) else ({'id':f's{i+1}', 'weight_kg':None, 'price_per_kg':None} if group == 'sales' else {'id':f"{'d' if group == 'deductions' else 'e'}{i+1}", 'category':'other', 'amount_krw':None})
            if group == 'sales':
                for field in ['weight_kg', 'price_per_kg']: item[field] = st.session_state.get(token + group + field + str(i), item[field])
            else:
                item['category'] = st.session_state.get(token + group + 'cat' + str(i), item['category'])
                item['amount_krw'] = st.session_state.get(token + group + str(i), item['amount_krw'])
            raw[group].append(item)
    try: pending = normalize(raw) != v
    except (TypeError, ValueError): pending = True
    if pending: _revoke(ws)
    return pending


def render_shipments(ws, disabled=False):
    store = ws.shipment
    ws.session.shipment = store
    with st.expander("실제 출하 기록·정산 검산 · 개인 확장"):
        st.caption("출하 전 비교와 분리한 한 건의 기록입니다. 카드 검산은 키 없이 됩니다. 모르는 금액은 빈칸, 실제 무료임을 확인한 경우에만 0을 입력하세요. 원문 정산서·이름·계좌·연락처는 입력하지 않습니다.")
        st.caption("개인 기록 JSON/PDF에는 입력 금액이 포함됩니다. 공식 제출·공개 시연은 가상 자료만 사용하세요. 서버 자동 저장·은행 확인·당시 가정 대조는 하지 않습니다.")
        if not store.snapshot():
            options = [x['id'] for x in ws.store.scenario.get('options', [])]
            index = st.selectbox("실제로 선택한 출하 대안", list(range(len(options) + 1)),
                format_func=lambda x: "즉시 출하" if x == 0 else f"보관 대안 {x}", key="shipment_choice")
            chosen = {'kind':'immediate', 'option_id':None} if index == 0 else {'kind':'delayed', 'option_id':options[index - 1]}
            if st.button("현재 비교와 출하 기록 연결", disabled=disabled):
                try:
                    store.connect(ws.export_case("출하 전 기준"), chosen)
                    _revoke(ws)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        else:
            original = store.snapshot()["baseline"]
            st.caption(f"연결한 원래 비교 내용 해시: {original['snapshot_sha256'][:16]}… · 정산 수정과 별도로 보존")
            v = store.values()
            token = f"shipment_{store.snapshot()['record_id']}_{store.revision}"
            raw = deepcopy(v)
            raw["input_kind"] = st.selectbox("정산 기록의 구분", list(KINDS), index=list(KINDS).index(v["input_kind"]), format_func=KINDS.get, key=token + "_kind")
            for field, label in SCALARS.items():
                raw[field] = st.text_input(label, value=v[field] or "", key=token + field,
                    placeholder="YYYY-MM-DD" if field == "shipment_date" else "미확인: 빈칸")
            for group, title in [("sales", "같은 출하 건의 판매 행"), ("deductions", "정산서 공제"), ("external_costs", "정산 밖 비용")]:
                st.markdown("**" + title + "**")
                count = st.number_input(title + " 항목 수", min_value=0, max_value=12, value=len(v[group]), step=1, key=token + group)
                items = []
                for i in range(count):
                    item = deepcopy(v[group][i]) if i < len(v[group]) else ({"id": f"s{i+1}", "weight_kg": None, "price_per_kg": None} if group == "sales" else {"id": f"{'d' if group == 'deductions' else 'e'}{i+1}", "category": "other", "amount_krw": None})
                    if group == "sales":
                        for field, label in [("weight_kg", "판매중량(kg)"), ("price_per_kg", "단가(원/kg)")]:
                            item[field] = st.text_input(f"판매 행 {i+1} {label}", value=item[field] or "", key=token + group + field + str(i))
                    else:
                        item["category"] = st.selectbox(f"{title} {i+1} 범주", list(CATEGORIES), index=list(CATEGORIES).index(item['category']), format_func=CATEGORIES.get, key=token + group + "cat" + str(i))
                        item["amount_krw"] = st.text_input(f"{title} {i+1} 금액(원)", value=item['amount_krw'] or "", key=token + group + str(i))
                    items.append(item)
                raw[group] = items
            for field, label in FLAGS.items():
                raw[field] = st.checkbox(label, value=v[field], key=token + field)
            normalized, error = None, None
            try: normalized = normalize(raw)
            except (ValueError, TypeError): error = "날짜·숫자·항목을 확인하세요. 음수/반품/추가 정산은 첫 버전에서 지원하지 않습니다."
            dirty = normalized is None or normalized != v
            if dirty:
                ws.session.messages = []
                st.session_state.agent_messages = []
                st.session_state.pop("settlement_pdf", None)
                st.warning("정산 카드 편집 중입니다. 이전 검산·다운로드를 숨겼습니다. 아는 항목을 확인·저장하세요.")
            if st.button("정산 입력 확인·저장", disabled=disabled, key=token + "save"):
                if error: st.error(error)
                else:
                    try:
                        store.confirm(normalized, store.revision)
                        _revoke(ws)
                        st.rerun()
                    except ValueError as exc: st.error(str(exc))
            if not dirty and store.revision:
                result = reconcile(store)
                st.markdown(render_settlement(result, details=False))
                with st.expander("정산 근거 보기 · 입력과 계산"):
                    st.markdown(citation_details(result))
                st.download_button("개인 출하·정산 JSON 내려받기", store.export(), file_name="감귤_개인출하정산.json", mime="application/json", key="shipment_download")
                if st.button("정산 PDF 준비", disabled=disabled):
                    try:
                        from .pdf_report import render_pdf_report
                        st.session_state.settlement_pdf = (result['input_digest'], render_pdf_report([{"tool": "reconcile_settlement", "result": result}]))
                    except Exception:
                        st.error("정산 PDF를 만들지 못했습니다. 기록과 미리보기는 유지됩니다.")
                cached = st.session_state.get("settlement_pdf")
                if cached and cached[0] == result['input_digest']:
                    st.download_button("개인 정산 PDF 내려받기", cached[1], file_name="감귤_개인정산검산.pdf", mime="application/pdf", key="shipment_pdf_download")
                st.caption("‘확인한 정산 기록을 검산해줘’라고 대화로 요청할 수 있습니다. 카드에 저장한 정산 금액·계산 근거는 로컬에서 처리합니다. 채팅에 직접 쓴 내용과 기존 출하 비교 조건은 모델 API로 전송됩니다.")
            if st.checkbox("내려받기 여부를 확인했고 이 세션의 출하 기록을 삭제합니다", key="shipment_delete_check"):
                if st.button("출하 기록 삭제", disabled=disabled):
                    store.clear()
                    _revoke(ws)
                    st.session_state.pop("shipment_delete_check", None)
                    st.rerun()
        uploaded = st.file_uploader("개인 출하·정산 JSON 가져오기 · 현재 기록 대체", type=["json"], key="shipment_upload")
        accepted = st.checkbox("개인 기록 파일임을 확인했고 현재 출하 기록을 대체하는 데 동의합니다", key="shipment_import_confirm")
        if st.button("출하·정산 파일 검사 후 복원", disabled=disabled or uploaded is None or not accepted):
            try:
                # Parse is atomic; no display/execute of imported model text or cached citations.
                content = uploaded.getvalue()
                parse_shipment(content)
                store.restore(content, store.revision)
                _revoke(ws)
                st.session_state.pop("shipment_import_confirm", None)
                st.rerun()
            except ValueError as exc: st.error(str(exc))
