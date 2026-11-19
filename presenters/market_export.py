"""Canonical, offline serialization for the connected A/B + market result.

The historical shares are descriptive facts about daily same-spec auction means,
not a probability of receiving a price. Never infer missing trade specifications.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from services.market_decision import market_query_from_pair_result, market_spec_identity, threshold_observation_eligible

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
    record = stored.get("result") if isinstance(stored, dict) else None
    explicit_threshold_record = (isinstance(record, dict) and threshold_observation_eligible(pair_result)
                                 and (record.get("comparison") or {}).get("threshold_only") is True
                                 and stored.get("pair_version") == pair_result.get("future_pair_version"))
    if (pair_result.get("suppress_market_evidence") or pair_result.get("threshold_only")) and not explicit_threshold_record:
        used = pair_result.get("historical_price_lookup") == "used"
        return {"analysis_type": "market_decision",
                "status": "NOT_APPLICABLE" if used else "NOT_REQUESTED",
                "message": ("과거 가격쌍을 비교에 사용했습니다. 현재는 비용 차이가 미확인이라 별도 시장 근거 연결을 생략했습니다."
                            if used else "이 비교에서는 과거 가격·시장 근거를 조회하지 않았습니다.")}
    if pair_result.get("price_basis") != "gross":
        return {"analysis_type": "market_decision", "status": "NOT_COMPARABLE",
                "message": "수수료 차감 후 단가와 차감 전 경락가격은 직접 비교하지 않습니다."}
    if isinstance(record, dict) and record.get("analysis_type") == "market_decision":
        current = market_query_from_pair_result(pair_result)
        used = record.get("query") or (record.get("market_evidence") or {}).get("conditions")
        comparison = record.get("comparison") or {}
        saved_date = comparison.get("option_b_date")
        options = pair_result.get("options_b") or []
        date_matches = not saved_date or not options or options[0].get("sale_date") == saved_date
        saved_threshold = comparison.get("minimum_whole_krw_per_kg")
        current_threshold = pair_result.get("minimum_whole_krw_per_kg")
        threshold_matches = not saved_threshold or not current_threshold or str(saved_threshold) == str(current_threshold)
        if (date_matches and threshold_matches and
                (current is None or market_spec_identity(current) == market_spec_identity(used))):
            return record
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


def season_observation_rows(evidence):
    """Give the screen and report identical counts and data-coverage labels."""
    for item in evidence.get("seasons", ()):
        status = item.get("status", "UNKNOWN")
        label = STATUS_LABELS.get(status, status)
        days = item.get("days_clean", 0)
        count = "-"
        if status == "OK" and days:
            count = f"{item['days_above']} / {days}일"
            if not item.get("window_complete", False):
                label = "부분 관측기간"
        elif status == "NO_DATA":
            first, last = item.get("source_first_date"), item.get("source_last_date")
            start, end = item.get("window_start"), item.get("window_end")
            if all((first, last, start, end)) and (end < first or start > last):
                label = "해당 기간 관측 없음"
        yield item.get("season"), count, label


def observation_window_text(evidence):
    window = (evidence.get("conditions") or {}).get("window_days")
    if window is None:
        return ""
    return f"관측 범위 · B안 출하일 전후 {window}일" if window else "관측 범위 · B안 출하일과 같은 월·일"


def market_markdown(result: dict) -> str:
    """One source for PDF and Markdown preview; do not recalculate market data."""
    if not isinstance(result, dict):
        return "**다년도 시장 근거**\n\n분석 결과를 확인할 수 없습니다."
    status = result.get("status")
    if status not in ("OK", "NO_CLEAN_DAYS", "NO_DATA") or not result.get("market_evidence"):
        message = result.get("message") or STATUS_LABELS.get(status, "시장 근거를 조회하지 못했습니다.")
        return f"**시장 근거: {_safe(STATUS_LABELS.get(status, '미조회'))}**\n\n{_safe(message)}"

    comp = result.get("comparison") or {}
    evidence = result.get("market_evidence") or {}
    query = result.get("display_conditions") or evidence.get("conditions") or result.get("query") or {}
    if not isinstance(query, dict):
        query = {}
    names = {"market_code": "시장", "trade_type": "거래방식", "variety": "품종",
             "grade": "등급", "size": "크기", "package": "포장", "package_kg": "상자중량(kg)",
             "month_day": "비교 월일", "window_days": "전후 범위(일)"}
    spec = " · ".join(f"{label} {_safe(query[key])}" for key, label in names.items() if query.get(key) is not None)
    def date_label(value):
        if not value:
            return "날짜 미확인"
        parts = str(value).split("-")
        return f"{int(parts[1])}월 {int(parts[2])}일" if len(parts) == 3 else _safe(value)
    earlier = date_label(comp.get("option_a_date"))
    later = date_label(comp.get("option_b_date"))
    lines = ["**과거 동일규격 경매가격과 손익분기 조건**", "",
             f"{later} 출하가 {earlier} 출하보다 손해 보지 않으려면 최소 **{_money(comp.get('minimum_whole_krw_per_kg', result.get('threshold_krw_per_kg')))}원/kg**이 필요합니다."]
    if window := observation_window_text(evidence):
        lines.append(window)
    lines += ["", "| 시즌 | 손익분기 가격 이상 / 유효 관측일 | 자료 상태 |", "| --- | --- | --- |"]
    for season, count, label in season_observation_rows(evidence):
        lines.append("| " + " | ".join(map(_safe, (season, count, label))) + " |")
    difference = comp.get("difference_vs_a_krw")
    if difference is not None:
        lines.append("")
        decision = comp.get("decision_under_assumptions")
        if decision == "B_ASSUMED_HIGHER":
            lines.append(f"입력한 판매단가·비용 가정에서 {later} 출하가 {_money(difference)}원 높습니다.")
        elif decision == "A_ASSUMED_HIGHER":
            lines.append(f"입력한 판매단가·비용 가정에서 {earlier} 출하가 {_money(-Decimal(str(difference)))}원 높습니다.")
        else:
            lines.append("입력 가정의 비교금액은 같습니다.")
    lines += ["", "조회 규격: " + (spec or "미확인")]
    if evidence.get("source_partial"):
        lines.append("일부 시즌의 자료를 읽을 수 없어 해당 행에 자료 오류를 표시했습니다.")
    if url := (evidence.get("source") or {}).get("url"):
        lines.append(f"출처 · aT 전국 공영도매시장 경매원천정보 ({_safe(url)})")
    lines.extend(["", "- 확인사항: 일별 같은 규격의 평균 경락가격을 센 과거 기록입니다. 미래에 그 가격을 받을 확률이나 농가 실수취가격이 아닙니다.",
                  "- 확인사항: 실제 제시가격, 거래규격, 감모율, 보관비, 수수료·공제 항목, 출하 가능 여부를 확인해야 합니다."])
    if any(s.get("excluded_flagged_days", 0) for s in evidence.get("seasons") or []):
        lines.append("- 확인사항: 원천 품질 경고가 있는 날짜는 일수 집계에서 제외했습니다.")
    return "\n".join(lines)
