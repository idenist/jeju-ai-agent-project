"""과거 가격쌍 검증 후 독립 조회 도구를 공통 서비스에 연결합니다."""
from __future__ import annotations

from copy import deepcopy

from agents.common.data_contract import verify_integrated_price_sources
from agents.common.result_contract import normalize_tool_result
from agents.juneok.config import PROJECT_ROOT
from agents.jeonghyeon.price_history import DEFAULT_FILE, execute_tool as execute_legacy_tool


def _unavailable(verification: dict) -> dict:
    """가격 원본이 일치하지 않으면 과거 가격쌍을 절대 공개하지 않습니다."""
    return {
        "analysis_type": "price_history",
        "status": "DATA_ERROR",
        "source_status": "SOURCE_VERIFICATION_FAILED",
        "reason": verification.get("reason", "SOURCE_VERIFICATION_FAILED"),
        "message": "시장가격 원천과 과거 가격 비교 자료를 함께 검증하지 못해 조회를 중단했습니다. 자료 버전을 확인해 주세요.",
        "missing_fields": [],
        "warnings": ["검증에 실패한 가격 비교 결과는 표시하거나 미래가격 추정에 사용하지 않습니다."],
        "cautions": ["과거 경락가격은 미래가격 또는 농가 실수취가가 아닙니다."],
        "is_future_forecast": False,
    }


def get_integrated_price_history(arguments: dict) -> dict:
    """검증한 동결 가격쌍으로만 조회하고 원래 상태·경고를 보존합니다.

    자료의 해시·내용이 조회 전후 다르면 결과를 반환하지 않습니다.
    사용자 입력을 시나리오에 저장하거나 시장가격을 제시 단가로 대입하지 않습니다.
    """
    if not isinstance(arguments, dict):
        return {"analysis_type": "price_history", "status": "INVALID_INPUT",
                "source_status": "INVALID_INPUT", "reason": "INVALID_INPUT",
                "message": "과거 가격 조회 조건은 객체여야 합니다.", "is_future_forecast": False}

    before = verify_integrated_price_sources(PROJECT_ROOT)
    if before.get("status") != "OK" or before.get("full_content_verified") is not True:
        return _unavailable(before)

    original = execute_legacy_tool("get_price_history", deepcopy(arguments), DEFAULT_FILE)

    after = verify_integrated_price_sources(PROJECT_ROOT)
    if (after.get("status") != "OK" or after.get("full_content_verified") is not True
            or before.get("comparison_source_sha256") != after.get("comparison_source_sha256")
            or before.get("selected_market_sha256") != after.get("selected_market_sha256")):
        return _unavailable(after if after.get("status") != "OK" else
                            {"reason": "SOURCE_CHANGED_DURING_QUERY"})

    normalized = normalize_tool_result("get_price_history", original)
    # 원본의 수치·예시·품질 건수는 변경하지 않고 상태만 공통 계약으로 바꿉니다.
    return {
        **deepcopy(original),
        "analysis_type": "price_history",
        "status": normalized["status"],
        "source_status": normalized["source_status"],
        "reason": normalized["reason"],
        "message": normalized["message"],
        "missing_fields": normalized["missing_fields"],
        "warnings": normalized["warnings"],
        "cautions": normalized["cautions"],
        "source": normalized["source"],
        "source_verification": {
            "status": "OK",
            "source_sha256": after["comparison_source_sha256"],
            "observed_first_date": after["observed_first_date"],
            "observed_last_date": after["observed_last_date"],
            "verified_comparison_rows": after["verified_comparison_rows"],
            "verified_availability_rows": after["verified_availability_rows"],
        },
        "is_future_forecast": False,
    }


def price_history_model_receipt(result: dict) -> dict:
    """모델에는 조회의 처리 상태와 추가 확인 조건만 전달합니다.

    실제 관측 가격·변동률·원본 행은 로컬 Python 화면과 보고서에서만 표시합니다.
    이전 도구 호출 인수는 모델 대화 이력에 그대로 남으므로 조건을 반복하지 않습니다.
    """
    keys = ("status", "source_status", "reason", "message", "missing_fields",
            "choices", "available_intervals", "clean_pairs", "warning_pairs",
            "unavailable_pairs", "interval_days", "applied_defaults_note")
    return {key: deepcopy(result[key]) for key in keys if key in result}
