"""과거 가격 조회의 후속 조건만 명시적으로 이어받는 순수 변환 함수입니다.

조회 결과의 자동 적용값·선택 후보·실제 가격은 저장하거나 병합하지 않습니다.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation


QUERY_FIELDS = (
    "market_code", "variety", "grade", "size", "package", "package_kg",
    "trade_type", "interval_days", "base_date", "limit",
)
CLEARABLE_FIELDS = frozenset(QUERY_FIELDS) - {"limit"}
TEXT_FIELDS = frozenset({"market_code", "variety", "grade", "size", "package", "trade_type", "base_date"})


class MissingPreviousPriceQuery(ValueError):
    """이전에 조회한 과거 가격 조건이 없어 후속 조건을 합칠 수 없습니다."""


def resolve_price_history_query(arguments: dict, previous: dict | None) -> dict:
    """새 조회는 독립적으로, 후속 조회는 이전에 *입력한* 필드만 이어받습니다.

    ``follow_up``이 참인 경우에만 이전 값을 사용합니다. ``None``은 후속
    조회에서 기존 값을 유지하는 표기이며, 명시적 삭제는 ``clear_fields``만 허용합니다.
    """
    if not isinstance(arguments, dict):
        raise ValueError("과거 가격 조회 인수는 객체여야 합니다.")
    allowed = {*QUERY_FIELDS, "follow_up", "clear_fields", "finish_turn"}
    if set(arguments) - allowed:
        raise ValueError("허용되지 않은 과거 가격 조회 조건이 있습니다.")
    follow_up = arguments.get("follow_up", False)
    if type(follow_up) is not bool:
        raise ValueError("후속 조회 여부는 참 또는 거짓이어야 합니다.")
    clear_fields = arguments.get("clear_fields", [])
    if (not isinstance(clear_fields, list)
            or any(not isinstance(x, str) or x not in CLEARABLE_FIELDS for x in clear_fields)
            or len(set(clear_fields)) != len(clear_fields)):
        raise ValueError("삭제할 과거 조회 조건 목록을 확인해 주세요.")
    if not follow_up and clear_fields:
        raise ValueError("새 조회에서는 이전 조회 조건을 삭제할 수 없습니다.")
    if follow_up and (not isinstance(previous, dict)
                      or not any(previous.get(key) is not None for key in CLEARABLE_FIELDS)):
        raise MissingPreviousPriceQuery("이전에 사용한 과거 가격 조회 조건이 없습니다.")
    result = deepcopy(previous) if follow_up else {}
    for field in clear_fields:
        if arguments.get(field) is not None:
            raise ValueError("같은 조건을 변경하고 삭제하도록 동시에 요청했습니다.")
        result.pop(field, None)
    for field in QUERY_FIELDS:
        value = arguments.get(field)
        if value is None:
            continue
        if field in TEXT_FIELDS:
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
                raise ValueError("과거 가격 조회의 문자열 조건을 확인해 주세요.")
            value = value.strip()
        elif field in {"interval_days", "limit"}:
            maximum = 366 if field == "interval_days" else 10
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("비교 간격 또는 표시 건수는 허용 범위의 정수여야 합니다.")
        elif field == "package_kg":
            if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                raise ValueError("포장중량은 양수여야 합니다.")
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("포장중량은 양수여야 합니다.") from exc
            if not number.is_finite() or number <= 0:
                raise ValueError("포장중량은 유한한 양수여야 합니다.")
        result[field] = deepcopy(value)
    if result.get("limit") is None:
        result["limit"] = 3
    return result
