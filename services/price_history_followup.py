"""과거 가격 조회의 후속 조건만 명시적으로 이어받는 순수 변환 함수입니다.

조회 결과의 자동 적용값·선택 후보·실제 가격은 저장하거나 병합하지 않습니다.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re


QUERY_FIELDS = (
    "market_code", "variety", "grade", "size", "package", "package_kg",
    "trade_type", "interval_days", "base_date", "limit",
)
CLEARABLE_FIELDS = frozenset(QUERY_FIELDS) - {"limit"}
TEXT_FIELDS = frozenset({"market_code", "variety", "grade", "size", "package", "trade_type", "base_date"})
_COMPACT_SIZE = re.compile(r"(?<![가-힣])(?:특|상|등외)(?:등급)?\s+([소중대])(?:과)?\s+(?:상자|박스)(?![가-힣])")
_UNCERTAIN_SIZE = re.compile(r"말고|아닌|아니고|인지|헷갈|또는|혹은|둘\s*중")
_TRADE_MODES = {
    "경매": re.compile(r"(?<![가-힣])경매(?=\s*(?:방식|거래|만|로|[,.!?]|$))"
                     r"|거래\s*(?:방식|구분)\s*(?:은|는|[:=])?\s*경매(?!\s*(?:가격|시세))"),
    "정가수의": re.compile(r"(?<![가-힣])정가\s*[·/]?\s*수의(?:\s*매매)?"),
    "전자거래": re.compile(r"(?<![가-힣])전자\s*거래"),
    "직거래": re.compile(r"(?<![가-힣])직거래"),
}
_TRADE_EXCLUSION = re.compile(r"\s*(?:(?:방식|거래|만|은|는|을|를)\s*)*(?:말고|빼고|제외|아니)")
_SAME_SPEC = re.compile(r"^\s*(?:(?:그럼|이번에도)\s+)?(?:같은|동일(?:한)?)\s*(?:거래\s*)?규격(?:으로|로)")
_INTERVAL = r"\d{1,3}\s*일(?:\s*간격)?"
_INTERVAL_LIST = re.compile(rf"(?<![\d./-]){_INTERVAL}(?:\s*(?:과|와|,|·|및|랑|하고)\s*{_INTERVAL}){{1,3}}")


def requested_same_spec_intervals(question: str) -> tuple[int, ...]:
    """Recognize a list of requested intervals for an explicitly reused market spec."""
    if (not _SAME_SPEC.search(question) or not re.search(r"과거|경락", question)
            or re.search(r"수수료|손익|보관|출하량|\b[AB]안|말고|대신|또는|중\s*하나|하나만|월\s*\d|"
                         r"(?:시장|품종|등급|크기|포장|기준일)(?:은|는|을|를|만)", question, re.I)):
        return ()
    match = _INTERVAL_LIST.search(question)
    if match is None:
        return ()
    intervals = tuple(dict.fromkeys(int(value) for value in re.findall(r"\d+", match.group())))
    return intervals if len(intervals) >= 2 and all(1 <= days <= 366 for days in intervals) else ()


class MissingPreviousPriceQuery(ValueError):
    """이전에 조회한 과거 가격 조건이 없어 후속 조건을 합칠 수 없습니다."""


def _package_weight(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("포장중량은 양수여야 합니다.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("포장중량은 양수여야 합니다.") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("포장중량은 유한한 양수여야 합니다.")
    return number


def price_history_query_key(query: dict) -> tuple:
    """Use resolved input fields, independent of how the model encoded carryover."""
    weight = query.get("package_kg")
    return tuple(_package_weight(weight) if field == "package_kg" and weight is not None else query.get(field)
                 for field in QUERY_FIELDS)


def resolve_price_history_request(arguments: dict, previous: dict | None, *, question: str = "") -> tuple[dict, dict | None]:
    """모델이 전달한 중량 충돌을 조회 전에 확인 질문으로 바꿉니다."""
    if not isinstance(arguments, dict):
        raise ValueError("과거 가격 조회 인수는 객체여야 합니다.")
    query_arguments = dict(arguments)
    cleared = query_arguments.get("clear_fields", [])
    if question and isinstance(cleared, list):
        # 경락가격·경매가격은 조회 지표입니다. 거래방식은 사용자 원문에 명시된 경우만 반영합니다.
        modes = {mode for mode, pattern in _TRADE_MODES.items() for match in pattern.finditer(question)
                 if not _TRADE_EXCLUSION.match(question, match.end())}
        query_arguments.pop("trade_type", None)
        if len(modes) == 1 and "trade_type" not in cleared:
            query_arguments["trade_type"] = modes.pop()
    if query_arguments.get("size") is None and isinstance(cleared, list) and "size" not in cleared:
        sizes = set(_COMPACT_SIZE.findall(question))
        if len(sizes) == 1 and not _UNCERTAIN_SIZE.search(question):
            query_arguments["size"] = sizes.pop()
    conflicts = query_arguments.pop("conflicting_package_kg", None)
    # 모델이 충돌 목록을 빠뜨려도 포장 문구의 중량과 별도 입력이 다르면 조회하지 않습니다.
    from services.price_history import WEIGHT
    if conflicts is None:
        package = query_arguments.get("package")
        if isinstance(package, str):
            stated = {_package_weight(value) for value in WEIGHT.findall(package)}
            if query_arguments.get("package_kg") is not None and stated:
                stated.add(_package_weight(query_arguments["package_kg"]))
            if len(stated) >= 2:
                conflicts = sorted(stated)
    if conflicts is None:
        return resolve_price_history_query(query_arguments, previous), None
    if not isinstance(conflicts, list) or not 2 <= len(conflicts) <= 3:
        raise ValueError("서로 다른 포장 중량을 두 개 이상 알려주세요.")
    weights = [_package_weight(value) for value in conflicts]
    if len(set(weights)) != len(weights):
        raise ValueError("충돌한 포장 중량은 서로 다른 값이어야 합니다.")

    # 현재 턴이 고른 값과 이전 턴에서 승계된 값 모두 확인 전에는 사용하지 않습니다.
    query_arguments.pop("package_kg", None)
    query = resolve_price_history_query(query_arguments, previous)
    query.pop("package_kg", None)
    # '10kg 박스'를 그대로 기억하면 5kg로 정정한 뒤에도 다시 충돌합니다.
    # 포장 종류만 보존하고, 포장 중량 표기는 확인 전 조회조건에서 제거합니다.
    package = query.get("package")
    if isinstance(package, str) and WEIGHT.search(package):
        package_kind = WEIGHT.sub("", package).strip()
        if package_kind:
            query["package"] = package_kind
        else:
            query.pop("package", None)
    labels = " / ".join(f"{weight.normalize():f}kg" for weight in weights)
    return query, {
        "analysis_type": "price_history",
        "status": "NEEDS_INPUT",
        "source_status": "PACKAGE_WEIGHT_CONFLICT",
        "reason": "PACKAGE_WEIGHT_CONFLICT",
        "missing_fields": ["package_kg"],
        "message": f"포장 한 개 중량이 {labels}로 서로 다릅니다. 어느 중량으로 조회할지 확인해 주세요.",
        "is_future_forecast": False,
    }


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
            _package_weight(value)
        result[field] = deepcopy(value)
    if result.get("limit") is None:
        result["limit"] = 3
    return result
