"""Canonical reasons and recovery actions for an unavailable historical price."""

_REASONS = {
    "NO_OBSERVATION": "이 거래규격과 날짜 간격으로 비교할 과거 관측이 없어요.",
    "QUALITY_EXCLUDED": "같은 규격의 기록은 있지만 품질 기준을 통과한 가격쌍이 없어요.",
    "FILE_UNVERIFIED": "가격 자료 파일을 검증하지 못해 조회를 멈췄어요.",
}
_LOOKUP_ACTIONS = {
    "NO_OBSERVATION": "출하명세에 적힌 거래규격을 확인해 주세요.",
    "QUALITY_EXCLUDED": "출하처에 같은 규격의 실제 제안 단가를 확인해 주세요.",
    "FILE_UNVERIFIED": "자료 담당자에게 가격 파일 검증을 요청해 주세요.",
}
PRICE_GAP_EVIDENCE = (
    "conditions", "interval_days", "base_date_filter", "attempts", "clean_pairs",
    "warning_pairs", "unavailable_pairs", "warning_counts", "unavailable_reasons", "source",
    "requested_conditions", "source_verification",
)


def classify_price_gap(result):
    source_status = result.get("source_status") or result.get("status")
    if source_status == "NO_CLEAN_COMPARISON":
        return "QUALITY_EXCLUDED" if result.get("warning_pairs", 0) > 0 else "NO_OBSERVATION"
    if source_status == "NO_DATA":
        return "NO_OBSERVATION"
    if (source_status in {"DATA_ERROR", "SOURCE_VERIFICATION_FAILED"}
            or (source_status == "ERROR" and result.get("error_kind") == "DATA_ERROR")):
        return "FILE_UNVERIFIED"
    return None


def price_gap(code, *, comparison=False):
    return {
        "code": code,
        "reason": _REASONS[code],
        "next_action": ("나중 출하 단가를 직접 입력해 주세요." if comparison else _LOOKUP_ACTIONS[code]),
    }


def render_price_gap(result):
    gap = result["data_gap"]
    return gap["reason"] + "\n\n" + gap["next_action"]
