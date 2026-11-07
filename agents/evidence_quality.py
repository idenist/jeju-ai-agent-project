#!/usr/bin/env python3
"""V1-F02: 과거 가격 분석의 근거 범위와 정책상 품질 등급을 판정한다.

통계적 유의성이나 미래 이익 확률을 산출하는 기능이 아니다.
5건·3일·5구간 등의 숫자는 운영상의 초기 경고 기준으로만 사용한다.
"""
from __future__ import annotations

from datetime import date
from typing import Sequence

from agents.baseline import PriceObservation

# 기계 판정에는 안정적인 코드값, 사용자 설명에는 한국어 문구를 사용한다.
WARNING_MESSAGES = {
    "limited_history": "현재 확보한 거래 자료는 제한된 기간·단일 시장의 관측치이므로 전체 계절을 대표하지 않습니다.",
    "not_actual_storage": "동일한 감귤을 실제로 보관·재판매한 기록이 아니라 서로 다른 날짜의 시장가격을 이용한 가상 계산입니다.",
    "not_forecast": "과거의 이익 발생 구간 비율은 미래 이익 발생 확률이나 출하 추천을 뜻하지 않습니다.",
    "overlapping_windows_possible": "분석 구간이 서로 겹칠 수 있어 개별 날짜쌍을 독립 표본으로 해석할 수 없습니다.",
    "low_sample_pairs_excluded": "시작일 또는 종료일의 거래건수가 설정한 최소값보다 적은 구간을 제외했습니다.",
    "stale_price": "최근 가격 관측일이 분석 기준일보다 3일 이상 이전입니다. 실시간 시세가 아닙니다.",
    "insufficient_valid_pairs": "유효한 과거 구간이 5개 미만이라 사례 요약만으로 출하 결정을 뒷받침하기 어렵습니다.",
    "unknown_grade_or_size": "조회한 거래 규격의 등급·크기 중 원본 결측 표시(미상)가 포함되어 있습니다.",
}


def assess_evidence_quality(
    *,
    observations: Sequence[PriceObservation],
    candidate_pair_count: int,
    excluded_low_sample_pair_count: int,
    valid_pair_count: int,
    analysis_date: date,
    min_transactions_per_day: int,
) -> dict:
    """유효 날짜쌍 수·최신성·결측 여부로 설명 가능 범위를 구분한다.

    상태는 ``unavailable``(0구간), ``limited``(1~4구간),
    ``descriptive_only``(5구간 이상)이며 통계적 신뢰도 점수가 아니다.
    """
    if min_transactions_per_day < 1:
        raise ValueError("최소 거래건수는 1 이상이어야 합니다.")
    if candidate_pair_count < 0 or excluded_low_sample_pair_count < 0:
        raise ValueError("날짜쌍 건수는 음수일 수 없습니다.")
    if candidate_pair_count != valid_pair_count + excluded_low_sample_pair_count:
        raise ValueError("후보 수와 포함·제외 수의 합계가 일치하지 않습니다.")

    latest = observations[-1].date if observations else None
    earliest = observations[0].date if observations else None
    age = (analysis_date - latest).days if latest else None
    if age is not None and age < 0:
        raise ValueError("분석 기준일 이후의 관측값을 근거로 사용할 수 없습니다.")

    status = "unavailable" if valid_pair_count == 0 else "limited" if valid_pair_count < 5 else "descriptive_only"
    warnings = ["limited_history", "not_actual_storage", "not_forecast"]
    # 후보 구간이 2개 이상이라면 날짜가 실제로 겹치는지와 무관하게 독립표본 가정을 경계한다.
    if candidate_pair_count > 1:
        warnings.append("overlapping_windows_possible")
    if excluded_low_sample_pair_count:
        warnings.append("low_sample_pairs_excluded")
    if age is not None and age >= 3:
        warnings.append("stale_price")
    if valid_pair_count < 5:
        warnings.append("insufficient_valid_pairs")
    if any("GRADE_UNKNOWN" in o.quality_flags or "SIZE_UNKNOWN" in o.quality_flags
           for o in observations):
        warnings.append("unknown_grade_or_size")

    return {
        "status": status,
        "warnings": warnings,
        "warning_messages": {code: WARNING_MESSAGES[code] for code in warnings},
        "analysis_date": analysis_date.isoformat(),
        "observed_date_range": {
            "start": earliest.isoformat() if earliest else None,
            "end": latest.isoformat() if latest else None,
        },
        "last_observed_date": latest.isoformat() if latest else None,
        "days_since_last_observation": age,
        "evidence_scope": "서울가락시장(110001) 경매, 동일 품종·등급·크기·상자중량의 과거 관측가격",
        "min_transactions_per_day": min_transactions_per_day,
        "excluded_count": excluded_low_sample_pair_count,
        "statistical_confidence_level": None,
        "is_future_probability": False,
    }
