"""Local execution policy: explicit stop reasons, repeat detection and whole-turn trimming."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class ExecutionPolicy:
    model_requests: int = 6
    tool_calls: int = 12
    seconds: float = 90
    history_turns: int = 4
    input_characters: int = 32000
    identical_calls: int = 2


class RunStopped(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def encoded_size(items):
    def serialize(item):
        return item.model_dump(mode="json") if hasattr(item, "model_dump") else vars(item)
    return len(json.dumps(items, ensure_ascii=False, default=serialize))


def user_starts(items):
    return [i for i, item in enumerate(items) if isinstance(item, dict) and item.get("role") == "user"]


def bound_history(items, max_turns, max_characters):
    """Drop complete oldest turns only; never split the active call/output protocol."""
    removed = 0
    while True:
        starts = user_starts(items)
        if len(starts) <= max_turns and encoded_size(items) <= max_characters:
            return removed
        if len(starts) < 2:
            raise RunStopped("CONTEXT_LIMIT")
        del items[:starts[1]]
        removed += 1


class RepeatGuard:
    def __init__(self, allowed=2):
        self.allowed = allowed
        self.seen = {}

    def check(self, name, args, scenario, revision):
        normalized = deepcopy(args)
        if isinstance(normalized, dict):
            normalized.pop("finish_turn", None)
            normalized.pop("expected_version", None)
        # Logical state catches no-op writes even if the store's revision increments.
        raw = json.dumps([name, normalized, scenario, revision], sort_keys=True, ensure_ascii=False, default=str)
        signature = hashlib.sha256(raw.encode()).hexdigest()
        count = self.seen.get(signature, 0) + 1
        if count > self.allowed:
            raise RunStopped("REPEATED_TOOL_CALL")
        self.seen[signature] = count


def classify_failure(error, phase):
    if isinstance(error, RunStopped):
        return error.reason
    if phase != "model_request":
        return "INTERNAL_ERROR"
    from openai import APITimeoutError, APIConnectionError, AuthenticationError, PermissionDeniedError, RateLimitError
    if isinstance(error, APITimeoutError):
        return "API_TIMEOUT"
    if isinstance(error, APIConnectionError):
        return "CONNECTION_ERROR"
    if isinstance(error, (AuthenticationError, PermissionDeniedError)):
        return "AUTH_ERROR"
    if isinstance(error, RateLimitError):
        return "RATE_LIMITED"
    return "API_ERROR"


FAILURE_GUIDANCE = {
    "TIME_LIMIT": "요청 처리 시간 한도에 도달했습니다. 조건은 유지했습니다. 직접 비교하거나 요청을 나누어 다시 시도하세요.",
    "TOOL_LIMIT": "한 요청의 도구 실행 한도에 도달했습니다. 확인된 부분 결과까지만 사용하고 요청을 나누어 주세요.",
    "LIMIT_REACHED": "모델 요청 횟수 한도에 도달했습니다. 조건을 확인하고 직접 비교하거나 다시 요청하세요.",
    "REPEATED_TOOL_CALL": "같은 조건의 도구 호출이 진전 없이 반복돼 중단했습니다. 입력을 확인하거나 조건 카드로 직접 비교하세요.",
    "CONTEXT_LIMIT": "이번 요청의 맥락이 입력 한도를 넘었습니다. 조건은 유지했습니다. 대화만 비우거나 요청을 나누어 주세요.",
    "AUTH_ERROR": "모델 계정 인증·접근권한을 확인하세요. 조건 카드는 키 없이 사용할 수 있습니다.",
    "RATE_LIMITED": "모델 요청 제한에 도달했습니다. 잠시 뒤 다시 시도하거나 조건 카드를 사용하세요.",
    "API_TIMEOUT": "모델 응답이 시간 안에 도착하지 않았습니다. 조건은 유지했으므로 다시 시도하거나 직접 비교하세요.",
    "CONNECTION_ERROR": "모델 연결을 확인하세요. 조건 카드는 계속 사용할 수 있습니다.",
    "INTERNAL_ERROR": "실행 처리 중 오류가 발생했습니다. 확인된 조건을 유지했으므로 직접 비교하고 운영자에게 점검을 요청하세요.",
    "MODEL_INCOMPLETE": "모델 응답이 미완료로 끝났습니다. 확인된 부분 결과만 사용하고 다시 요청하세요.",
    "API_ERROR": "모델 요청을 완료하지 못했습니다. 설정·연결을 확인하거나 직접 비교하세요.",
}
