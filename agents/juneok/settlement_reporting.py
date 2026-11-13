"""Canonical, program-owned claim/citation rendering for settlement results."""
from decimal import Decimal
from .settlement import SCALARS, FLAGS, KINDS, CATEGORIES

LABELS = {"row_gross_krw": "판매 행 매출 합계", "gross_krw": "검산에 사용한 매출 G",
    "deductions_krw_known": "입력된 정산서 공제 소계", "external_krw_known": "입력된 정산 밖 비용 소계",
    "expected_net_krw": "검산 지급액 G-D", "statement_delta_krw": "정산 차액 N-(G-D)",
    "after_entered_costs_krw": "입력 비용 반영금액 N-E", "received_delta_krw": "입력 입금액과 지급액의 차이"}


def field_label(path):
    if path in SCALARS: return SCALARS[path]
    if path in FLAGS: return FLAGS[path]
    if path == "input_kind": return "실제값·가정 구분"
    parts = path.split(".")
    group = {"sales": "판매 행", "deductions": "정산서 공제", "external_costs": "정산 밖 비용"}[parts[0]]
    return group + " " + parts[1] + " " + {"weight_kg": "중량(kg)", "price_per_kg": "단가(원/kg)", "category": "범주", "amount_krw": "금액(원)"}[parts[2]]


def value_text(value, path):
    if value is None: return "미확인"
    if type(value) is bool: return "확인" if value else "미확인"
    if path.endswith("category"): return CATEGORIES[value]
    if path == "input_kind": return KINDS[value]
    if path == "shipment_date": return value
    return format(Decimal(value), ",f")


def render_settlement(result, details=True):
    if "amounts" not in result:
        return result.get("message", "확인한 출하·정산 기록이 없습니다.")
    registry = result["citations"]
    lines = ["**출하 기록·정산 검산**", f"기록 버전 {result['record_revision']} · {KINDS[result['input_kind']]}",
             "입력 확인은 외부 사실 인증이 아닙니다. 금액 단위: 원, 합계 후 0.01원 반올림.", "", "| 항목 | 금액 | 근거 |", "| --- | --- | --- |"]
    for key, label in LABELS.items():
        amount = result["amounts"].get(key)
        if amount is not None:
            ref = result["amount_refs"][key]
            lines.append(f"| {label} | {Decimal(amount):,.2f} | [{ref}] |")
    for field in ["statement_net_krw", "received_krw"]:
        ref, entry = next((ref, entry) for ref, entry in registry.items() if entry.get("path") == field)
        lines.append(f"| {SCALARS[field]} | {value_text(entry['value'], field)} | [{ref}] |")
    lines += ["", "**확인할 사항**", *["- 확인사항: " + x for x in [*result['checks'], *result['pending']]]]
    if not result['checks'] and not result['pending']:
        lines.append("- 입력 정산 항등식과 입력 입금액이 일치합니다. 외부 사실 검증 결과는 아닙니다.")
    if result["imported_not_verified"]: lines.append("- 확인사항: 가져온 파일의 사용자 확인 표시는 작성자의 주장입니다. 원문 진위는 확인하지 않았습니다.")
    lines += ["", *["- 확인사항: " + w for w in result["warnings"]]]
    if details: lines += ["", citation_details(result)]
    return "\n".join(lines)


def citation_details(result):
    if "citations" not in result: return ""
    lines = ["**근거 목록 · 현재 기록 버전에 고정**", "", "| 근거 ID | 근거 내용 |", "| --- | --- |"]
    for ref, item in result["citations"].items():
        if item["kind"] == "input":
            detail = f"{field_label(item['path'])}: {value_text(item['value'], item['path'])}; 카드 입력·확인 v{item['confirmed_revision']} / {KINDS[item['input_kind']]}"
        else:
            detail = item["formula"] + "; " + ", ".join(f"[{x}]" for x in item["input_refs"])
        lines.append(f"| {ref} | {detail} |")
    lines += ["", f"- 계산 규칙 버전: {result['rule_version']}", f"- 입력 내용 해시: {result['input_digest']}",
              f"- 연결한 원래 비교 해시: {result['baseline_ref']['snapshot_sha256']}",
              "- 해시는 내용 동일성 확인용이며 자료·입력의 진위를 보증하지 않습니다."]
    return "\n".join(lines)
