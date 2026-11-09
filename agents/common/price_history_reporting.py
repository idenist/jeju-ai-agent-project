"""검증된 과거 경락가격 비교 결과의 채팅·보고서 공통 출력입니다."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


LABELS = {
    "market_code": "시장코드", "variety": "경매 품종", "grade": "등급", "size": "크기",
    "package": "포장 종류", "package_kg": "포장 중량(kg)",
    "trade_type": "거래 방식", "interval_days": "비교 간격(일)",
}
QUALITY = {
    "LOW_SAMPLE": "거래 건수 적음", "GRADE_UNKNOWN": "등급 미상",
    "SIZE_UNKNOWN": "크기 미상", "KEY_FIELD_UNKNOWN": "일부 거래조건 미상",
    "QUALITY_FLAG_MISSING": "품질 표시 누락", "LABEL_MISMATCH": "표시명 불일치",
}


def _text(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _number(value, decimals=2, signed=False) -> str:
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "확인 불가"
        return f"{number:+,.{decimals}f}" if signed else f"{number:,.{decimals}f}"
    except (InvalidOperation, ValueError, TypeError):
        return "확인 불가"


def render_price_history(result: dict) -> str:
    """미래가격 예측이나 손익 계산으로 오해하지 않도록 과거 사례만 출력합니다."""
    if not isinstance(result, dict):
        return "과거 가격 조회 결과를 확인할 수 없습니다."
    status = result.get("status")
    source_status = result.get("source_status")
    lines = ["**동일 거래조건의 과거 경락가격 비교**"]

    changes = result.get("normalized_conditions") or {}
    for field, values in changes.items():
        if isinstance(values, dict):
            lines.append(f"- 자료 표기 변환: {LABELS.get(field, _text(field))} "
                         f"{_text(values.get('from'))} → {_text(values.get('to') or '확인 필요')}")
    if result.get("applied_defaults_note"):
        lines.append("- 자료에서 유일해 적용한 조건: " + _text(result["applied_defaults_note"]))

    if source_status == "INTERVAL_NOT_AVAILABLE":
        lines.append("요청한 비교 간격의 자료가 없습니다.")
        lines.append("- 지원되는 간격: " + ", ".join(f"{n}일" for n in result.get("available_intervals", [])))
    elif status == "NEEDS_INPUT":
        lines.append("다음 과거 가격 조회 조건을 확인해 주세요.")
        for field in result.get("missing_fields", []):
            values = result.get("choices", {}).get(field, [])
            choices = f" (선택 가능한 예: {', '.join(_text(v) for v in values[:5])})" if values else ""
            lines.append("- " + LABELS.get(field, _text(field)) + choices)
        lines.append("제시된 후보는 자동으로 선택하지 않습니다.")
    elif source_status in {"OK", "NO_CLEAN_COMPARISON"} and status in {"OK", "NO_DATA"}:
        conditions = result.get("conditions") or {}
        visible_conditions = [f"{LABELS.get(field, field)}: {_text(conditions[field])}"
                              for field in LABELS if field in conditions]
        lines.append("- 조회 조건: " + " / ".join(visible_conditions))
        lines.append(f"- 비교 간격: {result.get('interval_days')}일 (정확한 달력 날짜)")
        if result.get("base_date_filter"):
            lines.append("- 기준일 제한: " + _text(result["base_date_filter"]))
        lines.append(f"- 경고 없는 비교쌍: {result.get('clean_pairs', 0)}개")
        lines.append(f"- 품질 경고 있는 비교쌍: {result.get('warning_pairs', 0)}개")
        lines.append(f"- 비교 불가: {result.get('unavailable_pairs', 0)}개")
        change = result.get("clean_change_range_pct")
        if isinstance(change, dict) and change.get("min") is not None:
            lines.append("- 경고 없는 과거 변동 범위: " + _number(change["min"], signed=True)
                         + "% ~ " + _number(change["max"], signed=True) + "%")
            examples = result.get("examples") or []
            if examples:
                lines.extend(["", "과거 관측 사례 (기준일 최신순):"])
                for row in examples:
                    lines.append(f"- {_text(row.get('base_date'))} → {_text(row.get('target_date'))}: "
                                 f"{_number(row.get('base_price_kg'))} → "
                                 f"{_number(row.get('target_price_kg'))}원/kg "
                                 f"({_number(row.get('price_change_pct'), signed=True)}%)")
        else:
            lines.append("경고 없는 비교쌍이 없어 변동 범위를 제시하지 않습니다. 경고 있는 자료를 정상 사례로 대체하지 않습니다.")
        for code, count in (result.get("warning_counts") or {}).items():
            lines.append(f"- 품질 경고 {_text(QUALITY.get(code, code))}: {count}건")
        for reason in result.get("unavailable_reasons") or []:
            lines.append(f"- 비교 불가 사유 {_text(reason.get('message'))}: {reason.get('count')}건")
    else:
        lines.append(_text(result.get("message", "가격 비교 자료를 조회하지 못했습니다.")))

    verification = result.get("source_verification")
    if isinstance(verification, dict) and verification.get("status") == "OK":
        lines.append("- 원천 관측 범위: " + _text(verification.get("observed_first_date"))
                     + " ~ " + _text(verification.get("observed_last_date")))
    if result.get("source"):
        lines.append("- 조회 자료: " + _text(result["source"]))
    for caution in result.get("cautions") or []:
        lines.append("- 해석 주의: " + _text(caution))
    lines.append("**과거 거래끼리 비교한 값입니다. 같은 감귤을 실제 보관한 결과가 아니며, 미래 가격·출하일·농가 실수취 수익을 예측하지 않습니다.**")
    return "\n".join(lines)
