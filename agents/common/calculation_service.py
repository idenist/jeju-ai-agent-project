"""통합 서비스의 출하 손익 계산을 하나의 엔진으로 전달합니다.

Juneok 계산 엔진만 실행하며, Seunghyuk 계산기는 독립 회귀 검증에 남겨 둡니다.
기존 화면·도구가 기대하는 반환 구조를 변경하지 않습니다.
"""
from __future__ import annotations

from agents.common.result_contract import normalize_tool_result
from agents.juneok.comparison import compare_shipment_options as _primary_calculate


def compare_shipment_options(scenario: dict) -> dict:
    """기존 계산기를 한 번 실행하고 공통 응답 상태 계약을 확인합니다.

    어댑터는 값을 재계산하지 않습니다. 정상 결과는 원본을 그대로 반환해
    기존 UI·보고서·상태 관리 코드와 호환되도록 합니다.
    """
    result = _primary_calculate(scenario)
    normalized = normalize_tool_result("compare_shipment_options", result)
    if normalized["status"] != result.get("status"):
        return {
            "status": "DATA_ERROR",
            "message": "출하 손익 계산 결과의 공통 상태 계약을 확인할 수 없습니다.",
        }
    return result
