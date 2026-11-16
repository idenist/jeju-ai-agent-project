"""Canonical, offline serialization for the connected A/B + market result.

The historical shares are descriptive facts about daily same-spec auction means,
not a probability of receiving a price. Never infer missing trade specifications.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

STATUS_LABELS = {
    "OK": "조회됨", "NO_DATA": "해당 규격 거래 없음", "NO_CLEAN_DAYS": "품질 경고로 비교 불가",
    "NOT_PREPARED": "시즌 가공본 없음", "AMBIGUOUS_CODES": "분류코드 모호",
    "DATA_ERROR": "가격 자료 오류", "INVALID_CALENDAR_DATE": "날짜 오류",
    "PARTIAL_DATA": "일부 관측", "NEEDS_SPEC": "거래규격 확인 필요",
    "NOT_COMPARABLE": "가격 기준이 달라 비교 불가", "NOT_REQUESTED": "조회하지 않음",
    "NOT_APPLICABLE": "추가 연결 생략",
}


def _safe(value):
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _money(value):
    try:
        num = Decimal(str(value))
        if not num.is_finite():
            return "-"
        return f"{num:,.0f}"
    except (InvalidOperation, ValueError, TypeError):
        return "-"


def export_market_record(pair_result: dict, stored: dict | None) -> dict:
    """Use only the joined analysis just rendered for this A/B result."""
    if pair_result.get("suppress_market_evidence"):
        used = pair_result.get("historical_price_lookup") == "used"
        return {"analysis_type": "market_decision",
                "status": "NOT_APPLICABLE" if used else "NOT_REQUESTED",
                "message": ("과거 가격쌍을 비교에 사용했습니다. 현재는 비용 차이가 미확인이라 별도 시장 근거 연결을 생략했습니다."
                            if used else "이 비교에서는 과거 가격·시장 근거를 조회하지 않았습니다.")}
    record = stored.get("result") if isinstance(stored, dict) else None
    if isinstance(record, dict) and record.get("analysis_type") == "market_decision":
        return record
    if pair_result.get("price_basis") != "gross":
        return {"analysis_type": "market_decision", "status": "NOT_COMPARABLE",
                "message": "수수료 차감 후 단가와 차감 전 경락가격은 직접 비교하지 않습니다."}
    return {"analysis_type": "market_decision", "status": "NEEDS_SPEC",
            "message": "비교할 실제 거래규격이 아직 확인되지 않아 과거 시장 근거를 산출하지 않았습니다."}


def report_entries(pair_result: dict, market_result: dict) -> list[dict]:
    return [{"tool": "compare_future_pair", "result": pair_result},
            {"tool": "market_decision", "result": market_result}]


def export_json(pair_result: dict, market_result: dict, prior_json: str | None = None) -> str:
    """Preserve chat's existing report payload while adding the current market result."""
    payload = None
    if prior_json is not None:
        try:
            candidate = json.loads(prior_json)
        except (TypeError, ValueError):
            candidate = None
        if isinstance(candidate, dict):
            payload = candidate.copy()
    if payload is None:
        payload = {"format": "jeju-future-pair", "version": 2}
    payload["result"] = pair_result
    payload["market_decision"] = market_result
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def market_markdown(result: dict) -> str:
    """One source for PDF and Markdown preview; do not recalculate market data."""
    if not isinstance(result, dict):
        return "**다년도 시장 근거**\n\n분석 결과를 확인할 수 없습니다."
    status = result.get("status")
    if status not in ("OK", "NO_CLEAN_DAYS"):
        message = result.get("message") or STATUS_LABELS.get(status, "시장 근거를 조회하지 못했습니다.")
        return f"**시장 근거: {_safe(STATUS_LABELS.get(status, '미조회'))}**\n\n{_safe(message)}"

    comp = result.get("comparison") or {}
    evidence = result.get("market_evidence") or {}
    query = evidence.get("conditions") or result.get("query") or {}
    if not isinstance(query, dict):
        query = {}
    names = {"market_code": "시장코드", "trade_type": "거래방식", "variety": "품종",
             "grade": "등급", "size": "크기", "package": "포장", "package_kg": "상자중량(kg)",
             "month_day": "비교 월일", "window_days": "전후 범위(일)"}
    spec = " · ".join(f"{label} {_safe(query[key])}" for key, label in names.items() if query.get(key) is not None)
    lines = ["**과거 동일규격 경매가격과 손익분기 조건**", "",
             f"B안 최소 필요가격: **{_money(comp.get('minimum_whole_krw_per_kg', result.get('threshold_krw_per_kg')))}원/kg**",
             f"B안 출하 예정일: {_safe(comp.get('option_b_date'))}"]
    difference = comp.get("difference_vs_a_krw")
    if difference is not None:
        decision = comp.get("decision_under_assumptions")
        if decision == "B_ASSUMED_HIGHER":
            lines.append(f"입력한 판매단가·비용 가정에서 B안 비교금액이 {_money(difference)}원 높습니다.")
        elif decision == "A_ASSUMED_HIGHER":
            lines.append(f"입력한 판매단가·비용 가정에서 A안 비교금액이 {_money(-Decimal(str(difference)))}원 높습니다.")
        else:
            lines.append("입력 가정의 비교금액은 같습니다.")
    lines += ["", "조회 규격: " + (spec or "미확인"), "",
              "| 시즌 | 정상 관측일 | 기준 이상 일수 | 해당 일수 비율 | 해당 날짜 거래중량 비율 | 상태 |",
              "| --- | --- | --- | --- | --- | --- |"]
    for item in evidence.get("seasons") or []:
        state = item.get("status", "UNKNOWN")
        label = ("부분 관측기간" if state == "OK" and not item.get("window_complete", False)
                 else STATUS_LABELS.get(state, state))
        days = item.get("days_clean", 0)
        above = item.get("days_above", 0)
        daily = item.get("daily_share_percent")
        kg = item.get("kg_share_percent")
        lines.append("| " + " | ".join([
            _safe(item.get("season")), _safe(days), _safe(above),
            _safe(daily) + "%" if daily is not None else "-",
            _safe(kg) + "%" if kg is not None else "-", _safe(label)]) + " |")
    lines.extend(["", "- 확인사항: 일별 동일규격 평균 경락가격으로 집계한 과거 관측일 비율이며, 미래 가격 달성 확률이나 개별 농가의 실수취가격이 아닙니다.",
                  "- 확인사항: 실제 제시가격, 거래규격, 감모율, 보관비, 수수료·공제 항목, 출하 가능 여부를 확인해야 합니다."])
    if any(s.get("excluded_flagged_days", 0) for s in evidence.get("seasons") or []):
        lines.append("- 확인사항: 원천 품질 경고가 있는 관측일은 비율에서 제외했습니다.")
    return "\n".join(lines)
