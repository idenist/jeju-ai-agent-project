"""Render evidence and calculation results without trusting model arithmetic."""
from __future__ import annotations

import json
import hashlib
from decimal import Decimal, InvalidOperation

FIELD_NAMES = {
    "shipment_variety": "출하통계 품종", "target_month": "조회 월", "start_year": "시작 연도", "end_year": "마지막 연도",
    "quantity_kg": "물량(kg)", "current_price_per_kg": "지금 판매 가능한 단가(원/kg)",
    "price_basis": "수수료 차감 전·후 기준", "cost_policy": "기타 비용 처리 기준",
    "included_costs": "단가에 이미 포함된 비용 확인", "current_fee_percent": "현재 수수료율(%)",
    "current_costs": "현재 별도 비용", "delay_days": "보관 일수", "loss_percent": "기간 전체 감모율(%)",
    "daily_storage_cost": "하루 추가 보관비(원)", "fee_percent": "대안 수수료율(%)", "costs": "대안 별도 비용",
    "options": "비교할 보관 대안", "storage_available": "저장 가능 여부", "shipping_flexible": "출하일 변경 가능 여부",
    "market_code": "시장코드", "variety": "경매 품종", "grade": "등급", "size": "크기",
    "package": "포장 종류", "package_kg": "포장중량(kg)",
}
TOOL_LABELS = {"compare_shipment_options": "출하 대안 비교", "compare_sensitivity": "내 가정 범위 비교", "get_historical_shipment_context": "제주 과거 출하통계",
               "get_market_evidence": "동일조건 시장 참고가격", "update_scenario": "비교 조건 변경",
               "build_review_brief": "출하 검토 브리핑", "explain_decision_conditions": "판단이 바뀌는 조건"}
TOOL_LABELS.update(explore_data="확보 자료 탐색", solve_offer_limits="제안단가의 허용 조건", review_case_changes="저장 비교의 변경 검토")
TOOL_LABELS.update(reconcile_settlement="출하 기록·정산 검산", get_price_history="과거 동일규격 경락가격 비교")
TOOL_LABELS["compare_future_pair"] = "두 미래 출하안 비교"
FIELD_NAMES.update(quoted_price_per_kg="제안 단가 또는 미래 단가 가정(원/kg)", option_id="검토할 보관 대안")
FIELD_NAMES.update(comparison_reference="변경 검토 기준으로 선택한 저장 JSON")


def _text(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _number(value, places=2) -> str:
    if value is None:
        return "미입력"
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "유효하지 않은 값"
        return f"{amount:,.{places}f}".rstrip("0").rstrip(".") if places else f"{amount:,.0f}"
    except (InvalidOperation, ValueError, TypeError):
        return _text(value)


def _field(field: str) -> str:
    parts = str(field).split(".")
    if len(parts) == 3 and parts[0] == "options":
        return f"대안 {_text(parts[1])}: {FIELD_NAMES.get(parts[2], parts[2])}"
    return FIELD_NAMES.get(field, _text(field))


def _source_lines(source) -> list[str]:
    if not isinstance(source, dict):
        return ["", "출처: " + _text(source)]
    lines = ["", "출처·관측범위:"]
    for label, key in (("자료", "dataset"), ("자료 버전", "snapshot_id"), ("관측 시작일", "source_first_date"), ("관측 마지막일", "source_last_date"), ("단위", "unit")):
        if source.get(key) is not None:
            unit = "톤" if key == "unit" and source[key] == "ton" else source[key]
            lines.append(f"- {label}: {_text(unit)}")
    if source.get("url"):
        lines.append(f"- [원출처 확인]({source['url']})")
    return lines


def _input_lines(values: dict, varied=False) -> list[str]:
    basis = {"commission_net": "수수료 차감 후 단가", "gross": "수수료 차감 전 단가"}
    categories = {"commission": "수수료", "sorting": "선별비", "packaging": "포장비", "transport": "운송비", "storage": "보관비", "other": "기타"}
    lines = ["", "고정한 입력(감모·미래 단가는 위 표의 가정):" if varied else "계산에 사용한 입력:", "", "| 조건 | 값 |", "| --- | --- |",
             f"| 물량 | {_number(values.get('quantity_kg'))}kg |",
             f"| 지금 판매 가능한 단가 | {_number(values.get('current_price_per_kg'))}원/kg |",
             f"| 가격 기준 | {basis.get(values.get('price_basis'), '미확인')} |",
             "| 단가에 반영된 비용 | " + (", ".join(categories.get(x, _text(x)) for x in (values.get("included_costs") or [])) or "선택한 항목 없음") + " |"]
    if values.get("current_fee_percent") is not None:
        lines.append(f"| 현재 추가 차감 수수료율 | {_number(values['current_fee_percent'])}% |")
    lines.append("")
    def costs(items):
        return ", ".join(f"{categories.get(x['category'], x['category'])} {_number(x['amount_krw'])}원" for x in (items or [])) or "별도 비용 없음으로 확인"
    if values.get("cost_policy") == "itemized":
        lines.append("- 현재 별도 비용: " + costs(values.get("current_costs")))
    for option in values.get("options", []):
        period = f"{option['delay_days']}일" if varied else f"{option['delay_days']}일 전체 감모 {_number(option['loss_percent'])}%"
        lines.append(f"- {_text(option['id'])}: {period}, 하루 보관비 {_number(option['daily_storage_cost'])}원")
        if option.get("fee_percent") is not None:
            lines.append(f"  - 추가 차감 수수료율: {_number(option['fee_percent'])}%")
        if option.get("future_price_per_kg") is not None and not varied:
            lines.append(f"  - 사용자가 지정한 미래 단가 가정: {_number(option['future_price_per_kg'])}원/kg")
        if values.get("cost_policy") == "itemized":
            lines.append("  - 별도 비용: " + costs(option.get("costs")))
    return lines


def _warnings(result: dict) -> list[str]:
    output = []
    for label, field in (("가정", "assumptions"), ("확인사항", "warnings"), ("자료 범위", "cautions")):
        values = result.get(field, [])
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            output.extend(f"- {label}: {_text(value)}" for value in values)
    return output


def _render_future_pair(result: dict) -> str:
    a = result["option_a"]
    b = result["options_b"][0]
    basis = "수수료 차감 전" if result["price_basis"] == "gross" else "수수료 차감 후"
    lines = [
        f"물량 {_number(result['quantity_kg'])}kg · 보관 시작 {result['start_date']} · 단가 {basis}",
        f"A안 {a['sale_date']} 출하 · B안 {b['sale_date']} 출하 · 두 출하일 간격 {result['interval_days']}일",
        "", "| 조건 | A안 | B안 |", "| --- | --- | --- |",
        f"| 보관일 | {a['delay_days']}일 | {b['delay_days']}일 |",
        f"| 기간 전체 감모 | {_number(a['loss_percent'])}% | {_number(b['loss_percent'])}% |",
        f"| 판매 가능 물량 | {_number(a['remaining_kg'])}kg | {_number(b['remaining_kg'])}kg |",
        f"| 하루 보관비 | {_number(a['daily_storage_cost'])}원 | {_number(b['daily_storage_cost'])}원 |",
        f"| 별도 비용 | {_number(a['other_cost_krw'])}원 | {_number(b['other_cost_krw'])}원 |",
    ]
    if result["price_basis"] == "gross":
        lines.append(f"| 수수료율 | {_number(a['fee_percent'])}% | {_number(b['fee_percent'])}% |")
    lines += [
        "", "| 대안 | 가정 단가 | 비교금액 | A안 대비 차액 |", "| --- | --- | --- | --- |",
        f"| A안 | {_number(a['price_per_kg'])}원/kg | {_number(a['proceeds_krw'])}원 | 기준 |",
    ]
    for row in result["options_b"]:
        label = result["evidence"][row["id"]].get("label")
        title = f"B안 {_text(label)}" if label else "B안"
        lines.append(f"| {title} | {_number(row['price_per_kg'])}원/kg | {_number(row['proceeds_krw'])}원 | "
                     f"{_number(row['difference_vs_a_krw'])}원 |")
    lines += ["", f"**A안 대비 B안 손익분기 최소 단가: {_number(result['minimum_whole_krw_per_kg'], 0)}원/kg**",
              "", "**가정 단가 근거**", ""]
    lines += [f"- {_text(item['summary'])}" for item in result["evidence"].values()]
    sample = result["evidence"][b["id"]]
    if sample.get("base_date_range"):
        start, end = sample["base_date_range"]
        source = sample["source"].get("comparison_file")
        conditions = " / ".join(f"{FIELD_NAMES.get(k, k)} {_text(v)}" for k, v in sample["conditions"].items())
        lines += [f"- 과거가격 조회 조건: {conditions}", f"- 관측 기준일 범위: {start} ~ {end}",
                  f"- 비교 자료: {_text(source)}"]
    return "\n".join([*lines, *_warnings(result)])


def _render_result_body(result: dict) -> str:
    """Return canonical Korean Markdown from a Python tool result only."""
    if not isinstance(result, dict):
        return "결과 형식을 확인할 수 없습니다."
    status = result.get("status", "UNKNOWN")
    if result.get("analysis_type") == "future_pair":
        return _render_future_pair(result)
    if result.get("analysis_type") == "price_history":
        from agents.common.price_history_reporting import render_price_history
        return render_price_history(result)
    if result.get("analysis_type") == "settlement":
        from .settlement_reporting import render_settlement
        return render_settlement(result)
    if result.get("analysis_type") == "review_brief":
        labels = {"OK": "확인 완료", "NO_DATA": "관측자료 없음", "NEEDS_INPUT": "추가 입력 필요", "INVALID_INPUT": "입력 확인", "DATA_ERROR": "자료 오류"}
        lines = ["**" + ("선택한 검토 항목 완료" if result["review_status"] == "COMPLETE" else "부분 검토 · 확인할 항목 남음") + "**", _text(result["message"]),
                 "| 검토 항목 | 처리 상태 |", "| --- | --- |"]
        lines += [f"| {TOOL_LABELS.get(x['tool'], _text(x['tool']))} | {labels.get(x['status'], _text(x['status']))} |" for x in result["component_statuses"]]
        return "\n".join([*lines, *_warnings(result)])
    if result.get("analysis_type") == "decision_conditions":
        labels = {"HIGHER": "지금보다 비교금액 높음", "LOWER": "지금보다 비교금액 낮음", "EQUAL": "같음", "PRICE_UNKNOWN": "미래 단가 미입력"}
        limits = result["minimum_range"]
        lines = [f"대안: {_text(result['option_id'])}", f"입력한 가정 안의 원 단위 최소 단가: {_number(limits['low'], 0)} ~ {_number(limits['high'], 0)}원/kg",
                 "| 기간 전체 감모 | 가정 미래 단가(원/kg) | 최소 단가(원/kg) | 지금 대비 차이(원) | 조건부 비교 |",
                 "| --- | --- | --- | --- | --- |"]
        lines += [f"| {_number(r['loss_percent'])}% | {_number(r['future_price_per_kg'])} | {_number(r['minimum_whole_krw_per_kg'], 0)} | {_number(r['difference_krw'])} | {labels[r['outcome']]} |" for r in result["cases"]]
        lines += ["다음 확인: 실제 제시가격·기간 전체 감모·보관비·별도 비용을 확인하고 같은 기준으로 다시 비교하세요.",
                  "실행 가능성: " + {"CONFLICT": "실행 제약 충돌 · 금액과 별도로 제약부터 확인", "UNCONFIRMED": "미확인 · 저장시설·계약·납기 확인", "CONFIRMED_BY_USER": "사용자가 확인함 · 외부 사실 검증 아님"}[result["feasibility"]["status"]], *_warnings(result)]
        return "\n".join(lines)
    if status != "OK":
        labels = {"NEEDS_INPUT": "추가 입력이 필요합니다", "NEEDS_CLARIFICATION": "조회 조건을 확인해 주세요",
                  "NO_DATA": "조회 가능한 자료가 없습니다", "INVALID_INPUT": "입력값을 확인해 주세요",
                  "DATA_ERROR": "데이터를 확인해 주세요", "ERROR": "작업을 완료하지 못했습니다"}
        lines = [f"**{labels.get(status, '결과 상태를 확인해 주세요')}**", "", _text(result.get("message", result.get("error", status)))]
        if result.get("missing_fields"):
            lines += ["", "필요한 항목:"] + [f"- {_field(field)}" for field in result["missing_fields"]]
        if result.get("available_varieties"):
            lines += ["", "자료에 있는 출하 품종: " + ", ".join(_text(x) for x in result["available_varieties"])]
        if result.get("source"):
            lines += _source_lines(result["source"])
        lines += ["", *_warnings(result)]
        return "\n".join(lines).strip()

    if result.get("analysis_type") == "data_catalog":
        lines = ["**확보 자료의 실제 선택 후보**", f"조건에 맞는 후보 {result['total_candidates']}개 · 시작 {result['offset'] + 1} · 현재 표시 {len(result['candidates'])}개",
                 "| 선택 조건 | 실제 관측 범위/월 | 관측일 수 | 품질 확인 |", "| --- | --- | --- | --- |"]
        for item in result["candidates"]:
            condition = " / ".join(f"{FIELD_NAMES.get(k, '연도' if k == 'year' else k)}: {_text(v)}" for k, v in item["conditions"].items())
            period = f"{item['first_observed']} ~ {item['last_observed']}" if "first_observed" in item else f"{item['conditions']['year']}-{item['conditions']['target_month']:02d}"
            flags = [*item["warnings"], *( ["동명이코드 확인 필요"] if item.get("ambiguous_codes") else [])]
            lines.append(f"| {condition} | {period} | {item['observed_days']} | {_text(', '.join(flags) or '별도 경고 없음')} |")
        if result.get("next_offset") is not None: lines.append(f"다음 후보 시작 offset: {result['next_offset']}")
        return "\n".join([*lines, *_source_lines(result["source"]), *_warnings(result)])
    if result.get("analysis_type") == "offer_limits":
        label = "기간 전체 감모율" if result["solve_for"] == "loss_percent" else "전체 물량 하루 추가 보관비"
        kinds = {"FINITE_LIMIT": "유한한 허용 상한", "NO_FEASIBLE_VALUE": "가능한 비음수 값 없음", "ALL_VALID_VALUES": "허용 영역 전체에서 경제성 조건 충족"}
        lines = ["**제안 단가 기준 허용 조건**", f"대안 {_text(result['option_id'])} · {result['delay_days']}일 · 제안 단가/가정 {_number(result['quoted_price_per_kg'])}원/kg",
                 f"역산 항목: {label} · {kinds[result['boundary_kind']]}", f"대상 영역: {result['domain']}"]
        if result["boundary_kind"] == "FINITE_LIMIT":
            lines += [f"보수적으로 내린 표시 상한: **{result['conservative_limit']} {result['unit']}**",
                      f"산식 경계(소수 표시): {_text(result['exact_boundary'])} {result['unit']}"]
        lines += [f"고정한 기간 전체 감모: {_number(result['fixed_loss_percent'])}% · 고정한 하루 보관비: {_number(result['fixed_daily_storage_cost'])}원 (역산한 항목은 미입력 표시)",
                  "실행 가능성: " + {"CONFLICT": "실행 제약 충돌", "UNCONFIRMED": "미확인", "CONFIRMED_BY_USER": "사용자가 확인함"}.get(result["feasibility"]["status"], "확인 필요")]
        values = dict(result["input"])
        selected = dict(next(o for o in values["options"] if o["id"] == result["option_id"]))
        selected[result["solve_for"]] = None
        selected["future_price_per_kg"] = result["quoted_price_per_kg"]
        values["options"] = [selected]
        lines += ["역산 항목의 기존 값은 고정하지 않았습니다.", *_input_lines(values), *_warnings(result)]
        return "\n".join(lines)
    if result.get("analysis_type") == "case_changes":
        lines = ["**저장한 비교와 현재 조건의 변경 검토**", f"기준: {_text(result['saved_name'])} · 저장시각 {_text(result['saved_at'])}",
                 f"자료 버전 변경: {'있음' if result['data_changed'] else '없음'} · 당시 보관 결과 {result['archived_result_count']}건 (현재 결과로 사용하지 않음)",
                 "| 달라진 조건 | 저장 조건 | 현재 조건 |", "| --- | --- | --- |"]
        def cell(value):
            if value is None: return "미확인/없음"
            return _text(json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value)
        lines += [f"| {_field(c['field'])} | {cell(c['before'])} | {cell(c['after'])} |" for c in result["changes"]]
        if not result["changes"]: lines.append("| 조건 변경 없음 | 동일 | 동일 |")
        lines += ["", "**두 조건을 현재 계산 규칙으로 다시 계산한 결과**", "| 대안 | 저장 조건 최소단가 | 현재 조건 최소단가 | 차이(원/kg) |", "| --- | --- | --- | --- |"]
        lines += [f"| {_text(o['option_id'])} | {_number(o['saved_minimum'], 0)} | {_number(o['current_minimum'], 0)} | {_number(o['difference_per_kg'])} |" for o in result["outcomes"]]
        for label, key in [("저장", "saved_recalculated"), ("현재", "current_recalculated")]:
            output = result[key]
            if output["status"] != "OK": lines.append(f"{label} 조건: {output['status']} · 필요한 입력 " + ", ".join(_field(x) for x in output.get("missing_fields", [])))
        if not result["directly_comparable"]: lines.append("가격 또는 비용 기준이 달라 단가 차이를 산출하지 않았습니다.")
        return "\n".join([*lines, *_warnings(result)])
    if "explanation" in result:
        return "**조건과 자료 이해하기**\n\n" + _text(result["explanation"])
    if result.get("analysis_type") == "sensitivity":
        lines = ["**사용자가 정한 가정 범위의 비교**", "",
                 f"대안 {_text(result['option_id'])} · 보관기간 {result['delay_days']}일 고정 · 지금 출하 비교금액 {_number(result['current_proceeds_krw'])}원", "",
                 "| 기간 전체 감모 | 가정 미래 단가 | 판매 가능 물량 | 원 단위 최소 단가 | 가정 비교금액 | 지금 대비 차이 |",
                 "| --- | --- | --- | --- | --- | --- |"]
        for row in result["rows"]:
            lines.append(f"| {_number(row['loss_percent'])}% | {_number(row['future_price_per_kg'])} | {_number(row['remaining_kg'])}kg | {_number(row['minimum_whole_krw_per_kg'], 0)}원/kg | {_number(row['hypothetical_proceeds_krw'])} | {_number(row['difference_krw'])} |")
        lines += ["", "가격 단위는 원/kg, 금액·차이는 원입니다. 미래 단가를 지정하지 않은 행은 필요한 최소 단가만 계산합니다.",
                  "실행 가능성: " + {"CONFIRMED_BY_USER": "사용자가 확인함", "UNCONFIRMED": "미확인", "CONFLICT": "실행 제약 있음"}.get(result["feasibility"]["status"], "확인 필요"),
                  *_input_lines({**result["base_input"], "options": [o for o in result["base_input"]["options"] if o["id"] == result["option_id"]]}, varied=True), *_warnings(result)]
        return "\n".join(lines).strip()
    if "current_proceeds_krw" in result:
        lines = ["**입력 조건에 따른 출하 대안 비교**", "",
                 f"지금 출하 비교금액: **{_number(result['current_proceeds_krw'])}원**", "",
                 "| 대안 | 기간 | 판매 가능 물량 | 추가 보관비 | 손익분기 단가 | 원 단위 최소 단가 |",
                 "| --- | --- | --- | --- | --- | --- |"]
        for option in result.get("options", []):
            lines.append(f"| {_text(option.get('id', '대안'))} | {_text(option['delay_days'])}일 | {_number(option['remaining_kg'])}kg | {_number(option['storage_cost_krw'])}원 | {_number(option['break_even_price_per_kg'])}원/kg | **{_number(option['minimum_whole_krw_per_kg'], 0)}원/kg** |")
        hypothetical = [o for o in result.get("options", []) if o.get("hypothetical_proceeds_krw") is not None]
        if hypothetical:
            lines += ["", "사용자가 지정한 미래 단가 가정에서의 비교:", "",
                      "| 대안 | 가정 비교금액 | 지금 대비 차이 |", "| --- | --- | --- |"]
            for option in hypothetical:
                lines.append(f"| {_text(option['id'])} | {_number(option['hypothetical_proceeds_krw'])}원 | {_number(option['difference_krw'])}원 |")
        else:
            lines += ["", "미래 단가가 입력되지 않아 미래 비교금액과 추천 순위를 제시하지 않습니다."]
        feasibility = result.get("feasibility", {})
        label = {"CONFLICT": "실행 제약 충돌", "UNCONFIRMED": "실행 가능성 미확인",
                 "CONFIRMED_BY_USER": "사용자가 실행 가능 여부를 확인함"}.get(feasibility.get("status"), "미확인")
        lines += ["", f"실행 가능성: **{label}**"]
        if feasibility.get("unknown"):
            lines.append("- 확인할 조건: " + ", ".join(_field(x) for x in feasibility["unknown"]))
        if feasibility.get("conflicts"):
            lines.append("- 충돌 조건: " + ", ".join(_field(x) for x in feasibility["conflicts"]))
        lines += ["", *_warnings(result), *_input_lines(result.get("input", {}))]
        return "\n".join(lines).strip()

    if "scenario" in result and "version" in result:
        lines = ["**비교 조건을 변경했습니다.**", f"조건 버전: {_text(result['version'])}"]
        if result.get("changed_fields"):
            lines.append("변경한 항목: " + ", ".join(_field(x) for x in result["changed_fields"]))
        if result.get("loss_reconfirmation_required"):
            lines.append("보관기간이 바뀌어 감모율을 다시 확인해야 하는 대안: " + ", ".join(_text(x) for x in result["loss_reconfirmation_required"]))
        return "\n\n".join(lines)

    lines = ["**확인된 자료 조회 결과**", ""]
    series = result.get("series")
    if isinstance(series, list) and series:
        headers = list(series[0])
        labels = {"year": "연도", "year_month": "관측 월", "shipment_tons": "출하량(톤)",
                  "observed_days": "관측일 수", "source_rows": "원천 행 수", "variety": "출하 품종", "warning": "자료 경고"}
        lines += ["| " + " | ".join(labels.get(x, _text(x)) for x in headers) + " |",
                  "| " + " | ".join("---" for _ in headers) + " |"]
        for row in series:
            def cell(key):
                value = row.get(key, "")
                if key == "warning" and isinstance(value, list):
                    names = {"PARTIAL_MONTH": "부분월", "MISSING_DATE_ROWS_EXCLUDED": "날짜 없는 기록 제외"}
                    return ", ".join(names.get(x, _text(x)) for x in value) or "추가 경고 없음"
                return _text(value)
            lines.append("| " + " | ".join(cell(x) for x in headers) + " |")
        summary = result.get("summary", {})
        if summary:
            lines += ["", f"완전 월 관측 {_text(summary.get('years_count', 0))}개 연도 요약: 최솟값 {_number(summary.get('min'))}톤 · 중앙값 {_number(summary.get('median'))}톤 · 최댓값 {_number(summary.get('max'))}톤"]
            if summary.get("excludes_partial_months"):
                lines.append("부분월은 위 요약에서 제외했습니다. 이 분포는 미래 출하량이나 가격의 예측이 아닙니다.")
    elif "price_per_kg" in result:
        lines += [f"최근 확보 관측 경락가격: **{_number(result['price_per_kg'])}원/kg**",
                  f"- 실제 관측일: {_text(result.get('observed_date', '미확인'))}",
                  f"- 거래중량: {_number(result.get('total_kg'))}kg · 거래 건수: {_text(result.get('transaction_count', '미확인'))}"]
        if result.get("quality_flags"):
            lines.append("- 원천 품질 경고: " + _text(result["quality_flags"]))
        conditions = result.get("input", {})
        labels = {"market_code": "시장코드", "variety": "경매 품종", "grade": "등급", "size": "크기", "package": "포장", "package_kg": "포장중량(kg)", "as_of": "조회 기준일"}
        lines.append("- 조회 조건: " + " · ".join(f"{labels.get(k, k)} {_text(v)}" for k, v in conditions.items() if v is not None))
    else:
        # Keep unrecognized evidence fields intact rather than guessing their units.
        lines += ["```json", json.dumps({k: v for k, v in result.items() if k not in {"warnings", "cautions", "assumptions"}}, ensure_ascii=False, indent=2), "```"]
    if result.get("source"):
        lines += _source_lines(result["source"])
    lines += ["", *_warnings(result)]
    return "\n".join(lines).strip()


def render_result(result: dict) -> str:
    body = _render_result_body(result)
    if result.get("analysis_type") == "settlement":
        return body
    context = result.get("ui_context") if isinstance(result, dict) else None
    if not context:
        return body
    description = "결과 구분: " if context.get("kind") == "evidence" else "입력 구분: "
    description += _text(context["input_context"])
    if context.get("kind") != "evidence":
        description += " (사용자 표시이며 사실 검증 아님)"
    lines = [f"**{_text(context.get('channel', '직접 비교'))} · 조건 버전 {_text(context['version'])}**", description]
    if context.get("changed_fields"):
        lines.append("직전 제출 이후 변경: " + ", ".join(_text(x) for x in context["changed_fields"]))
    lines += [body, "**다음 확인과 행동**"]
    if result.get("status") != "OK":
        lines.append("위의 부족하거나 잘못된 조건을 확인하고 다시 비교하세요. 모르는 값은 0으로 채우지 말고 직접 정한 가정과 구분하세요.")
    elif result.get("analysis_type") == "offer_limits":
        lines.append("실제 제안 단가·해당 기간 감모·보관비와 저장/계약 가능성을 확인하세요. 표시 상한은 다른 조건을 고정한 경계입니다.")
    elif "options" in result or result.get("analysis_type") == "sensitivity":
        lines.append("손익분기 단가는 기다렸을 때 필요한 가격 조건입니다. 미래에 그 가격을 받을 것이라는 예측이 아닙니다.")
        feasibility = result.get("feasibility", {})
        if feasibility.get("status") == "CONFLICT":
            lines.append("저장 또는 출하일 변경에 제약이 있다고 입력했습니다. 계산 결과와 별도로 그 제약을 먼저 확인하세요.")
        elif feasibility.get("status") == "UNCONFIRMED":
            lines.append("저장시설 이용과 출하일 변경·계약·납기 조건을 관계자에게 확인하세요.")
        lines.append("실제 제시가격과 별도 비용을 확인해 비교하고, 감모·미래 단가가 달라질 때의 결과는 사용자 가정 범위로 확인하세요.")
    else:
        lines.append("자료의 관측기간·출처·단위를 확인하세요. 과거 관측을 현재 공급이나 미래가격으로 해석하지 않습니다.")
    return "\n\n".join(lines)


REPORT_FOOTER = "사용자 조건에 따른 비교 - 미래가격 예측 아님"
REPORT_NOTICES_HEADING = "이용 안내 및 확인사항"


def _report_fragment(result: dict) -> tuple[str, list[str]]:
    """Reorder canonical text for a report; do not change or recalculate any values."""
    body, notices = [], []
    for line in render_result(result).splitlines():
        stripped = line.strip()
        if stripped.startswith(("- 가정:", "- 확인사항:", "- 자료 범위:")):
            note = stripped[2:]
            # The common scope statement belongs in the footer, once per page.
            if note == "확인사항: 사용자가 제공한 조건에 따른 비교이며 미래가격 예측이 아닙니다.":
                continue
            note = note.replace("과거 신고 출하량이며 현재 실시간 수급 또는 미래가격 예측이 아닙니다.",
                                "과거 신고 출하량이며 현재 실시간 수급이 아닙니다.")
            notices.append(note)
            continue
        if stripped == "손익분기 단가는 기다렸을 때 필요한 가격 조건입니다. 미래에 그 가격을 받을 것이라는 예측이 아닙니다.":
            notices.append("산출 해석: 손익분기 단가는 보관 후 비교금액이 현재 출하와 같아지기 위해 필요한 가격 조건입니다.")
            continue
        if stripped == "미래 단가가 입력되지 않아 미래 비교금액과 추천 순위를 제시하지 않습니다.":
            line = "미래 단가 미입력: 필요한 최소 단가만 산출했습니다."
        line = line.replace(" (사용자 표시이며 사실 검증 아님)", "")
        if stripped in {"**입력 조건에 따른 출하 대안 비교**", "**출하 기록·정산 검산**"}:
            continue
        if stripped == "계산에 사용한 입력:":
            line = ""
        if stripped == "출처·관측범위:":
            line = "**자료 출처 및 관측범위**"
        body.append(line)
    return "\n".join(body).strip(), notices


def render_report(results: list[dict]) -> str:
    """A traceable report with canonical values and consolidated end notes."""
    fingerprint = hashlib.sha256(json.dumps({"format_version": 2, "results": results},
        ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12].upper()
    settlement_only = bool(results) and all(x.get('result', x).get('analysis_type') == 'settlement' for x in results)
    title = "제주 감귤 출하·정산 검산 보고서" if settlement_only else "제주 감귤 출하 비교 보고서"
    sections = ["# " + title, f"문서 ID: CIT-{fingerprint}"]
    notices = []
    table_number = 0
    for index, entry in enumerate(results, 1):
        wrapped = isinstance(entry, dict) and isinstance(entry.get("result"), dict)
        result = entry["result"] if wrapped else entry
        label = TOOL_LABELS.get(entry.get("tool"), "자료와 계산 결과") if wrapped else "자료와 계산 결과"
        body, extra = _report_fragment(result)
        notices.extend(extra)
        lines = body.splitlines()
        captioned = []
        for i, line in enumerate(lines):
            if line.startswith("|") and i + 1 < len(lines) and lines[i + 1].startswith("| ---"):
                table_number += 1
                caption = "입력 조건" if line.startswith("| 조건 |") else label
                captioned += [f"**표 {table_number}. {caption}**", ""]
            captioned.append(line)
        sections += [f"## {index:02d}. {label}", "\n".join(captioned)]
    result_objects = [x.get("result", x) if isinstance(x, dict) else {} for x in results]
    if any("current_proceeds_krw" in x or "missing_fields" in x for x in result_objects):
        notices.insert(0, "입력 확인: 가격·비용·감모와 입력 구분은 사용자 제공값 또는 가정이며 외부 사실 검증을 뜻하지 않습니다.")
    if any("current_proceeds_krw" in x for x in result_objects):
        notices.append("산출 단위: 가격은 원/kg, 비교금액은 원, 물량은 kg입니다. 원 단위 최소 단가는 손익분기 단가를 올림한 값입니다.")
    if not notices:
        notices.append("자료 확인: 결과에 표시된 관측기간·단위·조회 조건과 원출처를 함께 확인하세요.")
    sections += [f"## {REPORT_NOTICES_HEADING}", "\n".join("- " + x for x in dict.fromkeys(notices)), REPORT_FOOTER]
    return "\n\n".join(sections).strip() + "\n"
