"""독립 Agent 도구의 반환값을 공통 형식으로 변환하는 읽기 전용 어댑터입니다.

숫자·출처·품질 정보는 재계산하지 않고 원래 결과를 복사하여 보존합니다.
이 파일은 실제 도구를 실행하거나 사용자 상태를 변경하지 않습니다.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


# 기존 기능과 연결하기 전에는 도구명을 명시적으로 관리합니다.
JUNEOK_TOOLS = frozenset({
    "reconcile_settlement", "review_case_changes", "explore_data",
    "solve_offer_limits", "build_review_brief", "explain_decision_conditions",
    "update_scenario", "compare_shipment_options", "compare_sensitivity", "compare_future_pair",
    "get_historical_shipment_context", "get_market_evidence", "get_price_history", "explain_term",
})
SEUNGHYUK_TOOLS = frozenset({"get_market_price", "compare_shipping"})
JEONGHYEON_TOOLS = frozenset({"get_price_history"})

COMMON_STATUSES = frozenset({
    "OK", "NEEDS_INPUT", "NO_DATA", "INVALID_INPUT", "DATA_ERROR",
    "PARTIAL", "CHECK_REQUIRED",
})

# 과거 가격 도구의 예외 상태를 섣불리 성공이나 잘못된 사용자 입력으로 바꾸지 않습니다.
JEONGHYEON_STATUS_MAP = {
    "OK": "OK",
    "NEEDS_CLARIFICATION": "NEEDS_INPUT",
    "INTERVAL_NOT_AVAILABLE": "NO_DATA",
    "NO_DATA": "NO_DATA",
    "NO_CLEAN_COMPARISON": "NO_DATA",
    "AMBIGUOUS_CODES": "DATA_ERROR",
}

DEFAULT_MESSAGES = {
    "OK": "조회 또는 계산을 완료했습니다.",
    "NEEDS_INPUT": "조회 또는 계산에 필요한 조건을 확인해 주세요.",
    "NO_DATA": "요청한 조건에 사용할 수 있는 자료가 없습니다.",
    "INVALID_INPUT": "입력 조건을 확인해 주세요.",
    "DATA_ERROR": "자료 또는 도구 결과를 검증할 수 없습니다.",
    "PARTIAL": "일부만 확인된 결과입니다.",
    "CHECK_REQUIRED": "추가 확인이 필요한 결과입니다.",
}


def _list_of_strings(value: Any) -> list[str]:
    """출력 계약의 문자열 목록만 복사하고 임의 값을 설명으로 꾸미지 않습니다."""
    return list(value) if isinstance(value, list) and all(isinstance(x, str) for x in value) else []


def _source_status_for_legacy(tool_name: str, result: dict) -> tuple[str, str, str | None]:
    """상태 필드가 없는 승혁님 V0 반환값을 명시적으로 해석합니다."""
    if "error" in result:
        # V0 오류는 종류를 구조화하지 않았으므로 사용자 탓이라고 분류하지 않습니다.
        return "DATA_ERROR", "LEGACY_ERROR", "UNCLASSIFIED_LEGACY_ERROR"

    if tool_name == "get_market_price":
        if result.get("has_price") is False:
            return "NO_DATA", "LEGACY_NO_PRICE", "NO_MATCHING_MARKET_PRICE"
        if (result.get("has_price") is True
                and isinstance(result.get("observed_date"), str)
                and isinstance(result.get("observed_price_krw_per_kg"), str)):
            return "OK", "LEGACY_SUCCESS", None
        return "DATA_ERROR", "LEGACY_MALFORMED", "MALFORMED_RESULT"

    if (isinstance(result.get("conditions"), dict)
            and isinstance(result.get("break_even_price_krw_per_kg"), str)
            and isinstance(result.get("current_price_krw_per_kg"), str)):
        return "OK", "LEGACY_SUCCESS", None
    return "DATA_ERROR", "LEGACY_MALFORMED", "MALFORMED_RESULT"


def _source_status_for_jeonghyeon(result: dict) -> tuple[str, str, str | None]:
    """정현님 가격쌍 조회의 원래 상태를 유지하며 공통 상태로 연결합니다."""
    raw_status = result.get("status")
    if not isinstance(raw_status, str):
        return "DATA_ERROR", "MISSING_STATUS", "MALFORMED_RESULT"
    if raw_status == "ERROR":
        kind = result.get("error_kind")
        if kind == "INVALID_INPUT":
            return "INVALID_INPUT", raw_status, "INVALID_INPUT"
        return "DATA_ERROR", raw_status, kind if kind == "DATA_ERROR" else "UNCLASSIFIED_SOURCE_ERROR"
    if raw_status not in JEONGHYEON_STATUS_MAP:
        return "DATA_ERROR", raw_status, "UNKNOWN_SOURCE_STATUS"
    if raw_status in {"OK", "NO_CLEAN_COMPARISON"}:
        # 실제 성공 결과에는 비교 조건·간격·품질별 쌍 개수가 존재해야 합니다.
        clean = result.get("clean_pairs")
        valid = (isinstance(result.get("conditions"), dict)
                 and type(result.get("interval_days")) is int
                 and type(clean) is int and clean >= 0
                 and type(result.get("warning_pairs")) is int
                 and type(result.get("unavailable_pairs")) is int
                 and isinstance(result.get("examples"), list))
        if not valid or (raw_status == "OK" and clean == 0) or (raw_status == "NO_CLEAN_COMPARISON" and clean != 0):
            return "DATA_ERROR", raw_status, "MALFORMED_RESULT"
    common = JEONGHYEON_STATUS_MAP[raw_status]
    return common, raw_status, raw_status if raw_status != common else None


def _source_status_for_juneok(result: dict) -> tuple[str, str, str | None]:
    """준억님 반환 상태 중 부분 결과·정산 확인 상태는 별도로 보존합니다."""
    raw_status = result.get("status")
    if not isinstance(raw_status, str):
        return "DATA_ERROR", "MISSING_STATUS", "MALFORMED_RESULT"
    if raw_status == "NEEDS_CLARIFICATION":
        return "NEEDS_INPUT", raw_status, "NEEDS_CLARIFICATION"
    if raw_status not in COMMON_STATUSES:
        return "DATA_ERROR", raw_status, "UNKNOWN_SOURCE_STATUS"
    return raw_status, raw_status, None


def normalize_tool_result(tool_name: str, result: dict) -> dict:
    """공통 결과를 반환합니다. 원본 결과와 입력 객체는 수정하지 않습니다.

    도구마다 상태명이 다른 문제만 처리합니다. 경매가격을 판매 단가로 바꾸거나,
    품질 경고를 제거하거나, 비어 있는 숫자를 0으로 채우지 않습니다.
    """
    if not isinstance(tool_name, str) or not isinstance(result, dict):
        raise ValueError("도구 이름은 문자열, 결과는 객체여야 합니다.")
    if tool_name in SEUNGHYUK_TOOLS:
        status, source_status, fallback_reason = _source_status_for_legacy(tool_name, result)
    elif tool_name in JEONGHYEON_TOOLS:
        status, source_status, fallback_reason = _source_status_for_jeonghyeon(result)
    elif tool_name in JUNEOK_TOOLS:
        status, source_status, fallback_reason = _source_status_for_juneok(result)
    else:
        raise ValueError(f"등록되지 않은 도구입니다: {tool_name}")

    raw_reason = result.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) and raw_reason else fallback_reason
    raw_message = result.get("message")
    if not isinstance(raw_message, str) or not raw_message.strip():
        # 구형 도구의 분류되지 않은 오류에는 로컬 파일 경로가 포함될 수 있습니다.
        # 입력 오류임이 확실한 경우에만 원래 오류 문구를 사용자 안내에 재사용합니다.
        raw_message = (result.get("error")
                       if source_status == "ERROR" and status == "INVALID_INPUT" else None)
    message = raw_message if isinstance(raw_message, str) and raw_message.strip() else DEFAULT_MESSAGES[status]
    if source_status == "NO_CLEAN_COMPARISON" and not result.get("message"):
        message = "경고 없이 비교할 수 있는 과거 가격쌍이 없습니다. 경고가 있는 거래를 깨끗한 사례로 취급하지 않습니다."
    if source_status == "INTERVAL_NOT_AVAILABLE" and not result.get("message"):
        message = "요청한 간격에 대해 생성된 과거 가격 비교 자료가 없습니다."

    # 불확실성·출처는 원본 필드를 손실 없이 보존합니다.
    warnings = _list_of_strings(result.get("warnings"))
    flags = _list_of_strings(result.get("quality_flags"))
    for flag in flags:
        detail = f"자료 품질 경고: {flag}"
        if detail not in warnings:
            warnings.append(detail)
    if isinstance(result.get("warning_pairs"), int) and result["warning_pairs"] > 0:
        warnings.append(f"품질 경고가 포함된 과거 비교쌍: {result['warning_pairs']}개")

    return {
        "contract_version": 1,
        "tool": tool_name,
        "status": status,
        "source_status": source_status,
        "reason": reason,
        "message": message,
        "missing_fields": _list_of_strings(result.get("missing_fields")),
        "warnings": warnings,
        "cautions": _list_of_strings(result.get("cautions")),
        "source": deepcopy(result.get("source")),
        "data": deepcopy(result),
    }
