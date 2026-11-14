"""실험용 ML 가격 추정의 대화 Agent 연결 — CLI에서 명시적으로 활성화한 경우만 사용.

- 모델은 거래규격·기준일·비용 조건을 추출한다. 가격·손익 숫자는 Python이 계산한다.
- 정식 Agent 도구 목록과 사용자 UI에는 기본 등록하지 않는다.
- 과거 기준일의 연구용 리플레이만 허용한다. 현재 가격인 것처럼 조용히 대체하지 않는다.
"""
from __future__ import annotations

import csv
import os
import re
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .forecast_bridge import (PRICE_FILE, SUPPORTED_DAYS, _read_source_prices,
                              compare_experimental, selected_experiment_source,
                              EXPERIMENT_DATASET_ENV)


def nullable(kind: str, **kwargs) -> dict:
    return {"type": [kind, "null"], **kwargs}


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


# [대화 입력 계약] 수수료·보관비·감모는 모델이 예측하는 값이 아니다.
# LLM은 사용자의 명시 입력을 추출만 하고, 누락 필드는 null로 유지한다.
OPTION_FIELDS = ("delay_days", "loss_percent", "daily_storage_cost", "other_cost_krw", "fee_percent")
SPEC_FIELDS = ("market_code", "variety", "grade", "size", "package", "package_kg", "trade_type")
OPTION_PATCH = obj({
    "delay_days": nullable("integer", minimum=1, maximum=365),
    "loss_percent": nullable("number", minimum=0, exclusiveMaximum=100),
    "daily_storage_cost": nullable("number", minimum=0),
    "other_cost_krw": nullable("number", minimum=0),
    "fee_percent": nullable("number", minimum=0, exclusiveMaximum=100),
    "loss_confirmed_for_period": {"type": "boolean", "description": "보관일을 바꾸며 새 기간 전체 감모율도 명시적으로 확인했을 때만 true"},
})
SPEC_PATCH = obj({
    "market_code": nullable("string", description="시장코드. 사용자가 시장을 지정하지 않았으면 null. 서울가락은 110001."),
    "variety": nullable("string", description="경매 세부 품종 예: 조생귤. 단순히 감귤/귤만 말하면 null. 품종 코드를 만들어내지 마세요."),
    "grade": nullable("string", description="사용자가 말한 등급 예: 특"),
    "size": nullable("string", description="사용자가 말한 크기 예: 소"),
    "package": nullable("string", description="포장 종류만 예: 상자"),
    "package_kg": nullable("number", exclusiveMinimum=0, description="포장 한 개의 중량 kg. 전체 물량과 구분."),
    "trade_type": nullable("string", description="사용자가 말한 거래방식. 모르면 null."),
})

# [운영 격리] 이 도구는 정식 TOOLS에 직접 추가하지 않는다.
# CLI에서 --experimental-forecast를 명시했을 때만 AgentSession이 등록한다.
# strict 함수 스키마의 모든 필드는 required이나, '모름'은 null로 표현한다.
EXPERIMENTAL_TOOL = {
    "type": "function",
    "name": "compare_experimental_forecast",
    "description": (
        "연구용 ML 모델로 경락가격을 3·7·14일 후에만 추정하고 기존 A/B 계산기에 연결합니다. "
        "CLI에서 연구용 예측을 활성화한 경우 일반적인 출하 시점 비교 질문에도 사용합니다. "
        "미래 판매가격을 사용자에게 묻지 않습니다. 새 비교는 new, 조건 보완은 edit. "
        "as_of는 실제로 관측된 과거 날짜 YYYY-MM-DD 또는 사용자가 '마지막 관측일 기준'을 명시한 경우 latest_observed. "
        "오늘을 자동으로 예전 관측일로 바꾸면 안 됩니다. 비용·감모를 추정하거나 0으로 채우지 않습니다. "
        "기존 출하 시나리오 상태와 혼합하지 않습니다. 사용자에게 실제 출하를 권하지 않습니다."
    ),
    "strict": True,
    "parameters": obj({
        "action": {"type": "string", "enum": ["new", "edit"]},
        "patch": obj({
            "as_of": nullable("string", description="YYYY-MM-DD 또는 명시적 '마지막 관측일'은 latest_observed"),
            "quantity": nullable("number", exclusiveMinimum=0, description="전체 출하 물량(kg)"),
            "price_basis": nullable("string", enum=["gross", "commission_net", None]),
            "spec": {"anyOf": [SPEC_PATCH, {"type": "null"}]},
            "option_a": {"anyOf": [OPTION_PATCH, {"type": "null"}]},
            "option_b": {"anyOf": [OPTION_PATCH, {"type": "null"}]},
        }),
        "finish_turn": {"type": "boolean", "description": "가격 추정 또는 조건 확인까지 실행했으면 true"},
    }),
}

# [역할 구분] 언어모델은 조건·도구 선택만 수행한다.
# 미래가격 예측과 손익 계산은 Python에서 실행하고 모델이 수치를 만들어내지 못하게 한다.
EXPERIMENTAL_PROMPT = """

[선택된 연구용 ML 가격 예측 모드 — CLI 전용]
이 CLI에서 실험 모드를 명시적으로 활성화한 경우, 사용자가 '조생귤 3톤인데 언제 팔까요?'처럼 짧게 출하 시점 비교를 물어도 compare_experimental_forecast 도구로 조건을 확인하세요. 정식 배포 모델이나 실시간 출하 추천이 아닙니다.
A/B 예상 단가를 사용자에게 입력하라고 요구하지 않습니다. Python 모델이 가격을 추정합니다.
이번 사용자 메시지에 명시된 조건만 patch에 넣고 이전 실험의 일부만 수정하면 action=edit를 사용하세요. 명시되지 않은 항목은 null로 두세요. '감귤/귤'처럼 세부 품종이 불분명하면 variety=null로 두고 되물으세요. '이번 주/다음 주'처럼 정확한 날짜가 없으면 보관일을 추측하지 마세요.
과거 기준일을 '오늘'로 바꾸거나, 오래된 마지막 관측일을 현재 가격처럼 사용하지 마세요. 명시된 날짜 또는 '마지막 관측일 기준'인 경우에만 기준일을 정합니다.
3일·7일·14일 이외의 보관일은 예측 불가이며, 10일을 임의 보간하지 않습니다.
거래규격의 코드·감모율·비용은 만들어내지 않습니다. 보관기간을 변경하면 사용자가 새 기간 감모율을 명시적으로 확인했을 때만 재사용합니다.
도구가 반환한 가격·금액은 모델에게 제공되지 않습니다. Python이 CLI 화면에 표시합니다. 응답을 생성하기 위해 같은 내용을 다시 말하거나 추가 도구를 호출할 필요가 없습니다. finish_turn=true로 종료하세요.
일반 가격조회나 기존 A/B 계산 요청에서는 기존 도구를 그대로 사용하세요.
"""


LABELS = {
    "as_of": "가격 자료 기준일(날짜 입력 또는 '마지막 관측일 기준'이라고 선택)",
    "quantity": "출하 물량(kg)", "price_basis": "가격 기준(수수료 차감 전/후)",
    "spec.variety": "경매 품종", "spec.grade": "경매 등급", "spec.size": "경매 크기",
    "spec.package": "포장 종류", "spec.package_kg": "포장 한 개 중량(kg)",
    "option_a.delay_days": "A안 보관일", "option_b.delay_days": "B안 보관일",
    "option_a.loss_percent": "A안 기간 전체 감모율(%)",
    "option_a.daily_storage_cost": "A안 전체 물량 하루 보관비(원)",
    "option_a.other_cost_krw": "A안 별도 비용(원)",
    "option_a.fee_percent": "A안 수수료율(%)",
    "option_b.loss_percent": "B안 기간 전체 감모율(%)",
    "option_b.daily_storage_cost": "B안 전체 물량 하루 보관비(원)",
    "option_b.other_cost_krw": "B안 별도 비용(원)",
    "option_b.fee_percent": "B안 수수료율(%)",
}


# [실제 장애 사례] 2026-11-14: 동일한 연구용 ML 질문을 반복하자 두 번째 응답이
# 일반 손익계산 도구를 선택했고, 이미 확인한 3톤까지 다시 질문했다.
# 질문 의도의 후속 상태를 서버에서 보존해 라우터의 도구 선택 실패를 방어한다.
def keep_experimental_route(question: str, draft: "ExperimentalForecastDraft") -> bool:
    """실험 모드에서 일상적인 출하 시점 비교와 그 후속 답변을 같은 도구로 보냅니다."""
    text = " ".join(question.casefold().split())
    # [재발 방지] 기존에는 '연구용 ML'이란 단어가 없으면 자유 도구선택으로 돌아가
    # '귤 언제 팔까요?'에도 일반 계산기가 단가부터 요구했다. 실험 모드에 한해 이를 고친다.
    # 사용자가 취소하거나 일반 기능으로 명시 전환한 경우에는 강제로 ML을 호출하지 않는다.
    switched = ("취소", "중단", "그만", "일반 모드", "일반 계산", "다른 질문",
                "새로운 질문", "과거 가격만", "과거 경락가격만", "출하통계만")
    if any(word in text for word in switched):
        return False
    # [후속 턴] 앞선 비교가 미완성이면 '수수료 차감 전입니다' 한 문장도
    # 기존 물량·기간·규격의 보완으로 해석한다. 날짜/가격은 새로 만들어내지 않는다.
    if draft.awaiting_input:
        # 진행 중 용어만 물어보는 경우에는 설명 도구 사용을 허용하고
        # 이전 ML 조건은 그대로 보존한다 (예: '감모율이 뭐예요?').
        concept = any(word in text for word in ("감모", "보관비", "수수료", "가격 기준"))
        explaining = any(word in text for word in ("뜻", "의미", "뭐예요", "뭔가요", "뭐죠", "설명"))
        if concept and explaining:
            return False
        return True
    # 기존의 명시적 ML 요청도 그대로 지원한다.
    markers = ("연구용 ml", "실험용 ml", "ml 모델", "ml 예측", "예측 모델", "연구용 예측")
    if any(word in text for word in markers) and any(
            word in text for word in ("비교", "출하", "가격", "계산")):
        return True
    # [일상 질문] '감귤 3톤인데 언제 팔까요?', '3일 뒤와 14일 뒤 중
    # 언제 출하할까요?'처럼 모델 이름을 모르더라도 목적이 출하 시점 비교면
    # 실험 도구를 연다. 단순 과거 가격 조회·출하량 통계·용어 설명은 제외한다.
    if any(word in text for word in ("출하통계", "출하량", "과거 가격", "과거 경락가격")):
        return False
    crop_or_shipping = any(word in text for word in
                           ("감귤", "귤", "조생", "한라봉", "천혜향", "레드향",
                            "출하", "보관", "판매", "팔까", "팔까요", "팔지"))
    asks_choice = any(word in text for word in
                      ("언제", "어느", "중에", "중에서", "vs", "비교", "나을", "낫",
                       "유리", "좋을", "좋나요", "팔까", "팔까요", "고민", "추천"))
    # '3일 뒤 / 14일 뒤'처럼 시점이 있거나, '언제'를 묻는 요청만 시작한다.
    # 이번 주/다음 주는 정확한 날짜로 바꾸지 않고 입력 확인을 요청한다.
    time_context = ("언제", "이번 주", "이번주", "다음 주", "다음주", "오늘",
                    "지금", "내일", "모레", "보관", "출하 시점", "출하 시기")
    has_timing = any(word in text for word in time_context) or bool(
        re.search(r"\d+\s*(?:일|주)\s*(?:뒤|후|간격|보관)", text))
    return crop_or_shipping and asks_choice and has_timing


# [상태 소유권] 정현님의 과거가격 조회 조건과 준억님의 운영 A/B 시나리오를
# 수정하지 않는 별도 Draft. 검증 실패도 사용자 입력값을 추측해 채우지 않는다.
class ExperimentalForecastDraft:
    """기존 출하 시나리오와 분리된 개인 세션의 연구용 입력."""

    def __init__(self):
        self.inputs = self._empty()
        self.version = 0
        self.awaiting_input = False

    @staticmethod
    def _empty():
        return {"as_of": None, "quantity": None, "price_basis": None,
                "spec": dict.fromkeys(SPEC_FIELDS),
                "option_a": dict.fromkeys(OPTION_FIELDS),
                "option_b": dict.fromkeys(OPTION_FIELDS)}

    def snapshot(self) -> dict:
        return {"version": self.version, "inputs": deepcopy(self.inputs)}

    # 원천 데이터 변경 시 기준일·규격을 포함한 보류 입력도 무효화한다.
    def reset(self):
        self.inputs = self._empty()
        self.version += 1
        self.awaiting_input = False

    def apply(self, arguments: dict) -> dict:
        if (not isinstance(arguments, dict) or arguments.get("action") not in {"new", "edit"}
                or not isinstance(arguments.get("patch"), dict)):
            return {"status": "INVALID_INPUT", "message": "실험 조건은 새 비교 또는 수정 형식으로 알려주세요."}
        action, patch = arguments["action"], arguments["patch"]
        if set(patch) - {"as_of", "quantity", "price_basis", "spec", "option_a", "option_b"}:
            return {"status": "INVALID_INPUT", "message": "허용되지 않은 실험 조건입니다."}
        # [중요] new는 모든 입력을 버린다. 앞선 LLM의 new/edit 오분류가
        # 연속 질문에서 조건 소실을 일으켰으므로 라우터의 교정과 함께 관리해야 한다.
        candidate = self._empty() if action == "new" else deepcopy(self.inputs)
        try:
            for key in ("as_of", "quantity", "price_basis"):
                if patch.get(key) is not None:
                    candidate[key] = patch[key]
            self._merge_spec(candidate["spec"], patch.get("spec"))
            for key in ("option_a", "option_b"):
                self._merge_option(candidate[key], patch.get(key))
            self._validate(candidate)
        except (ValueError, TypeError) as exc:
            return {"status": "INVALID_INPUT", "message": str(exc)}
        # 원자적 반영: 임시 사본에 입력을 합치고 검사한 후에만 저장한다.
        # 잘못된 입력 하나로 이전에 확인한 정상 조건을 훼손하지 않는다.
        if candidate != self.inputs:
            self.inputs = candidate
            self.version += 1
        result = self.evaluate()
        # 응답 표시가 끝나더라도 부족한 조건을 다음 대화에서 이어서 받습니다.
        # [라우팅 근거] 다음 턴에서도 같은 ML 실험을 계속해야 하는지 나타내는 상태.
        # 실험 결과가 성공하면 False가 되어 일반 도구 사용을 다시 허용한다.
        self.awaiting_input = result.get("status") in {"NEEDS_INPUT", "NO_DATA"}
        return result

    @staticmethod
    # null은 '이전 조건 유지'이고 빈 값은 명시적 조건이 아니다.
    # 단순 응답으로 기존 거래규격을 덮어쓰지 않는다.
    def _merge_spec(target: dict, patch: dict | None):
        if patch is None:
            return
        if not isinstance(patch, dict) or set(patch) - set(SPEC_FIELDS):
            raise ValueError("거래규격 필드가 잘못됐습니다.")
        for k, value in patch.items():
            if value is None:
                continue
            # '귤'은 품종 코드가 아니다. 조생/극조생 중 하나를 임의 선택하지 않는다.
            # 이전 세부 품종을 지웠다는 뜻일 수 있으므로 기존값도 남기지 않는다.
            if k == "variety" and isinstance(value, str) and value.strip() in {
                    "귤", "감귤", "밀감", "제주감귤", "제주 감귤", "제주귤"}:
                target[k] = None
            else:
                target[k] = value

    @staticmethod
    def _merge_option(target: dict, patch: dict | None):
        if patch is None:
            return
        if not isinstance(patch, dict) or set(patch) - {*OPTION_FIELDS, "loss_confirmed_for_period"}:
            raise ValueError("A/B 보관조건 필드가 잘못됐습니다.")
        confirmed = patch.get("loss_confirmed_for_period", False)
        if type(confirmed) is not bool:
            raise ValueError("새 보관기간 전체 감모율의 확인 여부가 필요합니다.")
        changed_days = (patch.get("delay_days") is not None and target["delay_days"] is not None
                        and patch["delay_days"] != target["delay_days"])
        # 보관기간이 14일→7일로 달라지면 기존 '기간 전체 감모율 4%'는
        # 그대로 적용할 근거가 없다. 재확인 없이는 지워야 안전하다.
        if changed_days:
            target["loss_percent"] = None
        for k in OPTION_FIELDS:
            value = patch.get(k)
            if value is not None:
                if k == "loss_percent" and changed_days and not confirmed:
                    continue
                target[k] = value

    @staticmethod
    def _validate(data: dict):
        if data["as_of"] is not None:
            if not isinstance(data["as_of"], str):
                raise ValueError("기준일 형식이 잘못됐습니다.")
            if data["as_of"] != "latest_observed":
                try:
                    d = date.fromisoformat(data["as_of"])
                    if data["as_of"] != d.isoformat():
                        raise ValueError
                except ValueError:
                    raise ValueError("기준일은 YYYY-MM-DD 또는 '마지막 관측일 기준'이어야 합니다.") from None
        if data["price_basis"] not in (None, "gross", "commission_net"):
            raise ValueError("수수료 차감 전·후 기준을 알려주세요.")
        for key in SPEC_FIELDS:
            v = data["spec"][key]
            if v is not None:
                if key == "package_kg":
                    if _positive(v) <= 0:
                        raise ValueError("포장 한 개의 중량은 양수여야 합니다.")
                elif not isinstance(v, str) or not v.strip():
                    raise ValueError("거래규격은 빈 값 없는 문자열이어야 합니다.")
        if data["quantity"] is not None and _positive(data["quantity"]) <= 0:
            raise ValueError("출하 물량은 양수여야 합니다.")
        for name in ("option_a", "option_b"):
            for key, v in data[name].items():
                if v is None:
                    continue
                if key == "delay_days":
                    if type(v) is not int or not 1 <= v <= 365:
                        raise ValueError("A/B 보관일은 1~365 사이 정수여야 합니다.")
                else:
                    x = _positive(v)
                    if x < 0 or (key in {"loss_percent", "fee_percent"} and x >= 100):
                        raise ValueError("감모율·수수료율·비용이 허용 범위를 벗어났습니다.")
        a, b = data["option_a"]["delay_days"], data["option_b"]["delay_days"]
        if a is not None and b is not None and a >= b:
            raise ValueError("B안 보관일은 A안보다 길어야 합니다.")
        if data["price_basis"] == "commission_net" and any(
                data[k]["fee_percent"] not in (None, 0) for k in ("option_a", "option_b")):
            raise ValueError("수수료 차감 후 단가에 수수료율을 다시 적용하지 않습니다.")

    # 분석 가능성은 단계별로 확인: 예측기간 → 필수 조건 → 기준일 정상 거래 → ML.
    # 불충분한 상황에서 가격 0이나 평균값으로 임의 대체하지 않는다.
    def evaluate(self):
        data = self.inputs
        a, b = data["option_a"]["delay_days"], data["option_b"]["delay_days"]
        # 학습된 모델은 기준일로부터 3·7·14일만 지원한다.
        # A/B 사이 간격이 아니라 '각 옵션의 기준일 이후 보관일'을 검사한다.
        if a is not None and b is not None and (a not in SUPPORTED_DAYS or b not in SUPPORTED_DAYS):
            return {"status": "NO_DATA", "message": "연구용 모델은 기준일부터 3·7·14일만 예측합니다. "
                    "10일 같은 간격을 임의 보간하지 않습니다.", "supported_days": [3, 7, 14]}
        # 빠진 필드를 즉시 열거하되 한 번에 전부 물을 필요는 없다.
        # 모델이 가격을 추정해도 실제 보관비·감모는 사용자 확인이 필수다.
        # [일상 질문 대응] 과거에는 출하일을 하나도 말하지 않아도 누락 항목에
        # 보관기간이 포함되지 않아, 수수료부터 묻고 정작 비교 날짜는 못 물었다.
        # 날짜 → 세부 품종 → 물량/자료 기준 → 비용 순으로 확인한다.
        missing = [f"{name}.delay_days" for name in ("option_a", "option_b")
                   if data[name]["delay_days"] is None]
        missing += [f"spec.{k}" for k in ("variety", "grade", "size", "package", "package_kg")
                    if data["spec"][k] is None]
        missing += [k for k in ("quantity", "as_of", "price_basis") if data[k] is None]
        missing += [f"{name}.{k}" for name in ("option_a", "option_b")
                    for k in OPTION_FIELDS if k != "delay_days" and data[name][k] is None
                    and (k != "fee_percent" or data["price_basis"] == "gross")]
        if missing:
            return {"status": "NEEDS_INPUT", "missing_fields": missing,
                    "message": "연구용 예측 비교를 진행하려면 다음 실제 조건을 확인해야 합니다."}
        try:
            as_of = self._resolve_date(data["as_of"])
            # 거래규격의 '조생귤/특/소' 표시명을 신뢰된 원본 코드로 역조회한다.
            # LLM이 variety_code/size_code를 추측하게 해서는 안 된다.
            resolved = _resolve_spec(data["spec"], as_of)
        except ValueError as exc:
            return {"status": "NO_DATA", "message": str(exc)}
        return compare_experimental(
            as_of=as_of, spec=resolved["spec"], quantity=data["quantity"],
            price_basis=data["price_basis"], option_a=data["option_a"], option_b=data["option_b"],
        ) | {"resolved_spec": resolved}

    @staticmethod
    def _resolve_date(value: str) -> date:
        if value != "latest_observed":
            return date.fromisoformat(value)
        prices = _read_source_prices(_experiment_price_file())
        if not prices:
            raise ValueError("정상 관측 가격이 없습니다.")
        return max(x["date"] for x in prices)


def _experiment_price_file() -> Path:
    """표시명 역조회와 모델 입력을 동일한 연구용 버전으로 고정한다."""
    # [트러블슈팅] 기존 구현은 _resolve_spec/_resolve_date가 항상 공용 가격표를
    # 참조했다. 예측만 새 자료를 읽으면 다른 날짜·규격의 원천 코드를 섞게 된다.
    # 실험용 묶음을 지정하지 않은 기존 테스트·CLI는 원래의 PRICE_FILE을 사용한다.
    return (selected_experiment_source()["price_file"] if os.environ.get(EXPERIMENT_DATASET_ENV, "").strip()
            else PRICE_FILE)


def _positive(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("숫자에 true/false를 사용할 수 없습니다.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("금액·비율에는 유효한 숫자가 필요합니다.") from exc
    if not number.is_finite():
        raise ValueError("금액·비율에는 유한한 숫자가 필요합니다.")
    return number


# [원천 정합성] 동일 표시명에 여러 코드가 있을 수 있다.
# 후보가 0개거나 2개 이상이면 숫자를 임의로 만들지 말고 명확한 조건을 요청한다.
def _resolve_spec(request: dict, as_of: date) -> dict:
    """표시명을 실제 기준일 정상 거래와 대조해 원천 코드를 얻는다. 모델은 코드를 추측하지 않는다."""
    candidates = {}
    # 사용자 자연어의 제한적 표기 변환만 적용한다. 임의의 품종 매핑 금지.
    label_alias = {"특등급": "특", "소과": "소", "중과": "중", "대과": "대", "박스": "상자"}
    filters = {key: label_alias.get(value, value) if isinstance(value, str) else value
               for key, value in request.items()}
    with _experiment_price_file().open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["date"] != as_of.isoformat() or row["quality_flag"] != "OK":
                continue
            if any(filters.get(k) is not None and (
                    _positive(filters[k]) != _positive(row[k]) if k == "package_kg" else str(filters[k]) != row[k])
                   for k in SPEC_FIELDS):
                continue
            key = (row["market_code"], row["variety_code"], row["grade_code"],
                   row["size_code"], row["package"], row["package_kg"], row["trade_type"])
            candidates[key] = row
    if not candidates:
        raise ValueError(f"{as_of.isoformat()}에는 해당 규격의 정상 경매가격이 없습니다. 기준일·규격을 확인해 주세요.")
    # 단가가 여러 규격에 걸쳐 모호하면 평균 내지 않는다.
    # 자료를 섞으면 비교 자체가 다른 상품 간 비교가 될 수 있다.
    if len(candidates) != 1:
        raise ValueError("해당 조건에 여러 원천 규격 코드가 있습니다. 시장·규격을 더 명확히 알려 주세요.")
    row = next(iter(candidates.values()))
    spec = {k: row[k] for k in ("market_code", "variety_code", "grade_code", "size_code", "package", "package_kg", "trade_type")}
    defaults = {k: row[k] for k in ("market_code", "trade_type") if request[k] is None}
    return {"spec": spec, "applied_data_defaults": defaults}


# [숫자 유출 차단] 모델에 확정 손익을 다시 제공하면 LLM이 숫자를 재가공하거나
# 잘못 옮길 수 있다. Python 출력은 전체 결과, LLM 피드백은 상태만 분리한다.
def model_receipt(result: dict) -> dict:
    """실험용 금액·가격·원천 JSON을 모델에게 주지 않는다."""
    keys = ("status", "message", "missing_fields", "supported_days", "reason")
    return {key: deepcopy(result[key]) for key in keys if key in result}


# 터미널의 숫자 표시도 모델 자유 문장이 아닌 이 함수에서 일원화한다.
# 여기의 금액은 연구용 실험이며 운영 출하 권고로 표현하지 않는다.
def format_experimental_result(result: dict) -> str:
    status = result.get("status")
    if status == "NEEDS_INPUT":
        missing = result.get("missing_fields", [])
        head = [LABELS.get(k, k) for k in missing[:3]]
        tail = [LABELS.get(k, k) for k in missing[3:]]
        message = "연구용 ML 비교를 위해 확인할 조건: " + ", ".join(head)
        if tail:
            # [사용성] 처음부터 십여 개 조건을 한꺼번에 나열하지 않는다.
            message += f"\n나머지 {len(tail)}개 항목은 답변에 따라 순서대로 확인하겠습니다."
        return message + "\n모르는 비용·감모는 임의로 0으로 정하지 않습니다."
    if status != "EXPERIMENTAL_ONLY":
        return "연구용 예측 불가: " + result.get("message", "입력·자료·모델을 확인해 주세요.")
    source, calculation = result["provenance"], result["calculation"]
    keys = sorted(source["forecasts"], key=int)
    a, b = source["forecasts"][keys[0]], source["forecasts"][keys[1]]
    net_a, net_b = calculation["option_a"], calculation["options_b"][0]
    def won(value) -> str:
        return f"{Decimal(str(value)):,.0f}원"
    lines = ["[연구용 ML 가격 예측 · 정식 출하 권고 아님]",
             f"경매 정산 기준일: {source['as_of']} (관측 후 {source['age_days']}일)",
             f"A안 {keys[0]}일 후 ({a['target_date']}): {a['price_krw_per_kg']}원/kg",
             f"B안 {keys[1]}일 후 ({b['target_date']}): {b['price_krw_per_kg']}원/kg",
             f"A안 가상 비교금액: {won(net_a['proceeds_krw'])}",
             f"B안 가상 비교금액: {won(net_b['proceeds_krw'])}",
             f"B안−A안 가상 차액: {won(net_b['difference_vs_a_krw'])}",
             f"B안이 A안 이상이 되는 최소 단가: {won(calculation['minimum_whole_krw_per_kg'])}/kg",
             "예측에 기초한 가상 비교일 뿐, 실제 출하일의 수익이나 최적 시점을 보장하지 않습니다."]
    # [검증 가능성] 사용자 출력에서도 이번 가격이 어느 검증 묶음에서 나왔는지
    # 남긴다. 공용 가격표와 연구용 신규 관측을 같은 것으로 오해하지 않게 한다.
    if source.get("price_dataset_id"):
        lines.insert(1, f"가격 데이터 버전: {source['price_dataset_id']} "
                        f"(최신 관측 {source.get('latest_market_date', '확인 불가')})")
    lines.extend("주의: " + item for item in source.get("warnings", []))
    return "\n".join(lines)
