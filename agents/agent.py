#!/usr/bin/env python3
"""제주 감귤 출하 시점 분석 Agent: Responses API Tool Calling 최소 구현.

이 파일은 LLM이 질문을 이해하고 계산 도구를 호출하는 부분만 담당한다.
가격 조회와 손익 계산은 기존 baseline.py 함수를 그대로 재사용한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

# `python agents/agent.py`와 `python -m unittest` 실행을 모두 지원한다.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.baseline import (  # noqa: E402
    DEFAULT_PRICES_FILE,
    calculate_break_even,
    fixed,
    load_price_observations,
    to_decimal,
)

MARKET_CODE = "110001"  # 현재 수집된 원본은 서울가락시장만 포함한다.

# 이 지침은 LLM에게 판단의 범위를 제한한다. 실제 계산·데이터 검증은 아래 Python 도구가 담당한다.
SYSTEM_PROMPT = """당신은 제주 감귤의 출하 시점 판단을 보조하는 AI Agent입니다.

원칙:
1. 가격을 묻거나 손익을 계산해야 한다면 반드시 제공된 도구를 호출하세요.
   과거 가격·수익·손익분기 수치를 스스로 만들거나 암산하지 마세요.
2. 가격 비교에는 품종·등급·크기·포장중량이 필요합니다. 출하 경제성 분석에는
   추가로 물량(kg), 지연일수, 하루 추가 보관비(원), 보관기간 전체 감량률(%)이
   필요합니다. 질문에 없는 값을 임의로 가정하지 말고 간결하게 물어보세요.
3. '3톤'은 3000kg, '7일 보관비 14만 원'은 하루 2만 원으로 변환할 수 있습니다.
   이외의 비용·감량률·등급·크기는 추측하지 마세요.
4. 도구가 제공하는 가격은 서울가락시장 경매 데이터의 '최근 관측가격'이며
   실시간 확정 가격, 생산자 실수취가격 또는 미래 예측값이 아닙니다.
5. 미래 가격을 알 수 없으므로 '무조건 지금 출하' 또는 '기다리는 게 이익'이라고
   확정적으로 추천하지 마세요. 손익분기 조건과 가정별 차이를 설명하세요.
6. 가격이 없거나 도구에서 오류가 나면 수치를 추측하지 말고 입력 조건 변경이나
   사용자가 알고 있는 가격의 입력을 요청하세요.
7. 사용자에게는 한국어로 짧고 명확하게 답하고, 분석에 사용한 관측일과
   가정한 보관 조건 및 데이터 한계를 포함하세요.
8. 최종 숫자 요약은 Python이 도구 결과에서 직접 출력하므로 단가를 다시 곱하거나
   금액을 재계산하지 마세요. 도구의 필드 간 숫자 차이를 임의로 정정하지 마세요.
"""


# 등록 가능한 도구의 JSON 인수 형식을 엄격히 제한해 예기치 않은 키가 섞이지 않도록 한다.
def tool_schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """Responses API의 엄격한 함수 호출 스키마를 구성한다."""
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


# 가격 조회에 필요한 최소 4개 규격. 이 정보가 없으면 서로 다른 상품 가격이 혼합될 수 있다.
PRICE_FIELDS = {
    "variety": {"type": "string", "description": "표준 감귤 품종명. 예: 조생귤, 극조생감귤"},
    "grade": {"type": "string", "description": "경매 등급. 예: 특, 상, 미상"},
    "size": {"type": "string", "description": "크기 등급. 예: 소, 중, 대, 미상"},
    "package_kg": {"type": "number", "description": "포장 한 개의 중량(kg). 예: 5"},
}

# 비교 도구는 규격뿐 아니라 물량·기간·비용·감량률을 추가로 요구한다.
# 엄격한 스키마에서는 현재/미래 가격도 키를 포함해야 하며, 모르면 null을 사용한다.
SHIPPING_FIELDS = {
    **PRICE_FIELDS,
    "quantity_kg": {"type": "number", "description": "출하 물량(kg). 3톤이라면 3000"},
    "delay_days": {"type": "integer", "description": "지연 출하 일수. 예: 7"},
    "daily_storage_cost": {"type": "number", "description": "하루 추가 보관비(원). 예: 20000"},
    "loss_percent": {"type": "number", "description": "보관기간 전체 감량·폐기 비율(%). 예: 2"},
    "current_price_per_kg": {
        "type": ["number", "null"],
        "description": "사용자가 알려준 현재 단가(원/kg). 없으면 null로 놓고 CSV 최근 관측가격 사용",
    },
    "future_price_per_kg": {
        "type": ["number", "null"],
        "description": "사용자가 명시한 지연 후 가정 단가(원/kg). 없으면 null. 미래 예측을 생성하지 말 것",
    },
}

# 이 이름을 LLM에 광고한다. 모델은 호출할 도구·인수만 결정하고 구현 코드는 실행하지 못한다.
TOOLS = [
    tool_schema(
        "get_market_price",
        "서울가락시장 감귤의 동일 품종·등급·크기·5kg 등 명시된 포장중량 기준 최근 관측 경락가격 조회",
        PRICE_FIELDS,
        list(PRICE_FIELDS),
    ),
    tool_schema(
        "compare_shipping",
        "사용자 보관 조건과 실제 경매가격을 사용해 지금 출하 vs 지연 출하 손익분기 가격을 계산. 미래 가격 예측은 하지 않음",
        SHIPPING_FIELDS,
        list(SHIPPING_FIELDS),
    ),
]


# 모든 도구가 같은 규격 검증·가격 필터를 공유해 서로 다른 결과를 내지 않도록 한다.
def _validated_options(args: dict, prices_file: Path):
    """가격 조회에서 사용할 조건을 검사하고 기존 베이스라인을 호출한다."""
    variety = str(args["variety"]).strip()
    grade = str(args["grade"]).strip()
    size = str(args["size"]).strip()
    if not all((variety, grade, size)):
        raise ValueError("품종·등급·크기를 모두 입력해 주세요.")
    package_kg = to_decimal(args["package_kg"], "포장중량")
    if package_kg <= 0:
        raise ValueError("포장중량은 0보다 커야 합니다.")
    # 단가 자체를 LLM이 만들지 않도록 실제 CSV의 일별 관측 목록을 조회한다.
    observations = load_price_observations(
        prices_file,
        variety=variety,
        grade=grade,
        size=size,
        package_kg=package_kg,
        market_code=MARKET_CODE,
    )
    return variety, grade, size, package_kg, observations


# 조회된 날짜별 기록을 LLM에 돌려줄 근거 데이터로 정리한다.
def _observation_info(observations: list, days: int | None = None) -> dict:
    """최근 거래와 정확히 N일 전 거래를 구분해서 반환한다."""
    if not observations:
        return {"has_price": False, "reason": "동일 조건에 해당하는 경매가격이 없습니다."}
    latest = observations[-1]
    info = {
        "has_price": True,
        "source": "전국 공영도매시장 경매원천정보 API를 가공한 daily_prices.csv",
        "market": latest.market,
        "observed_date": latest.date.isoformat(),
        "observed_price_krw_per_kg": fixed(latest.price_per_kg),
        "transactions": latest.transaction_count,
        "total_volume_kg": fixed(latest.total_kg, 2),
        "quality_flags": list(latest.quality_flags),
        "is_future_forecast": False,
    }
    # 보관 일수와 동일한 N일 전을 찾되, 거래일 기준 N번째 이전으로 대체하지 않는다.
    if days is not None:
        previous_date = latest.date - timedelta(days=days)
        previous = next((o for o in observations if o.date == previous_date), None)
        if previous:
            delta = (latest.price_per_kg / previous.price_per_kg - 1) * 100
            info["past_price_change"] = {
                "from_date": previous.date.isoformat(),
                "to_date": latest.date.isoformat(),
                "from_price_krw_per_kg": fixed(previous.price_per_kg),
                "to_price_krw_per_kg": fixed(latest.price_per_kg),
                "change_percent": fixed(delta),
                "is_future_forecast": False,
            }
        else:
            info["past_price_change"] = {
                "available": False,
                "reason": f"정확히 {days}일 전({previous_date.isoformat()}) 동일 조건 거래 없음",
            }
    return info


# 가격만 묻는 질문용. 계산 도구와 달리 사용자 물량·보관 조건은 요구하지 않는다.
def get_market_price(args: dict, prices_file: Path) -> dict:
    variety, grade, size, pkg, observations = _validated_options(args, prices_file)
    return {
        "conditions": {
            "variety": variety, "grade": grade, "size": size,
            "package_kg": fixed(pkg, 3), "market_code": MARKET_CODE,
        },
        **_observation_info(observations),
        "caution": "최근 관측 경락가격이며 실시간 가격·농가 실수취액이 아닙니다.",
    }


# 손익분기 비교용. 가격 조회가 내부에 포함되므로 가격 도구를 따로 호출할 필요가 없다.
def compare_shipping(args: dict, prices_file: Path) -> dict:
    variety, grade, size, pkg, observations = _validated_options(args, prices_file)
    latest = observations[-1] if observations else None
    # 실제 견적 단가를 사용자가 제공했다면 그 값을 우선하고, 없을 때만 CSV 관측가격을 쓴다.
    user_price = args["current_price_per_kg"]
    # 시장 거래가 없는 규격이라도 사용자 기준단가가 있으면 가정 계산을 진행할 수 있다.
    if user_price is None and latest is None:
        raise ValueError("동일 조건 경락가격이 없습니다. 실제 현재 단가(원/kg)를 알려 주세요.")
    actual_price = (
        to_decimal(user_price, "현재 입력가격")
        if user_price is not None else latest.price_per_kg
    )
    delay_days = args["delay_days"]
    if isinstance(delay_days, bool) or not isinstance(delay_days, int):
        raise ValueError("보관 일수는 정수여야 합니다.")
    # 미래 단가는 모델이 추측하지 않는다. 사용자가 직접 제시한 경우에만 시나리오에 사용한다.
    future_price = (
        to_decimal(args["future_price_per_kg"], "미래 가정가격")
        if args["future_price_per_kg"] is not None else None
    )
    # 계산식을 agent.py에 복제하지 않고 재현 가능한 순수 Python 함수에 위임한다.
    result = calculate_break_even(
        current_price_per_kg=actual_price,
        quantity_kg=to_decimal(args["quantity_kg"], "출하 물량"),
        delay_days=delay_days,
        daily_storage_cost=to_decimal(args["daily_storage_cost"], "하루 보관비"),
        loss_percent=to_decimal(args["loss_percent"], "누적 감량률"),
        future_price_per_kg=future_price,
    )
    # 원 단위로만 제시할 때 반올림 대신 올림해야 최소 손익분기 단가를 보장한다.
    whole = result.break_even_price_per_kg.quantize(Decimal("1"), rounding=ROUND_CEILING)
    data = {
        "conditions": {
            "market_code": MARKET_CODE,
            "market": "서울가락",
            "variety": variety,
            "grade": grade,
            "size": size,
            "package_kg": fixed(pkg, 3),
            "quantity_kg": fixed(to_decimal(args["quantity_kg"], "출하 물량"), 2),
            "delay_days": delay_days,
            "daily_storage_cost_krw": fixed(to_decimal(args["daily_storage_cost"], "하루 보관비"), 0),
            "loss_percent": fixed(to_decimal(args["loss_percent"], "누적 감량률"), 2),
        },
        "current_price_source": "사용자 입력" if user_price is not None else "CSV 최근 관측값",
        "current_price_krw_per_kg": fixed(actual_price, 2),
        "current_gross_krw": fixed(result.current_gross, 0),
        "remaining_kg": fixed(result.remaining_kg, 2),
        "extra_storage_cost_krw": fixed(result.extra_storage_cost, 0),
        "break_even_price_krw_per_kg": fixed(result.break_even_price_per_kg, 2),
        "minimum_integer_price_krw_per_kg": fixed(whole, 0),
        "required_increase_percent": fixed(result.required_rise_percent, 2),
        "observed_market_price": _observation_info(observations, delay_days),
        "is_future_forecast": False,
        "cautions": [
            "미래 가격은 예측하지 않았으며, 출하 시점의 우열을 확정할 수 없습니다.",
            "경락가격은 생산자가 실제 받는 금액과 다릅니다.",
            "보관비·감량률은 사용자 가정값이고 두 선택지의 공통 비용은 제외했습니다.",
        ],
    }
    # 이 필드는 조건부 가정 손익이다. 모델 예측값이나 실제 수익으로 표현하면 안 된다.
    if future_price is not None:
        data["future_hypothesis"] = {
            "assumed_price_krw_per_kg": fixed(future_price, 2),
            "delayed_sale_minus_storage_krw": fixed(result.future_net_if_given, 0),
            "difference_vs_now_krw": fixed(result.difference_if_given, 0),
            "is_future_forecast": False,
        }
    return data



# 응답의 수치는 반드시 Python 도구가 반환한 문자열을 인용하며, LLM이 재계산하지 않는다.
def format_verified_result(name: str, result: dict) -> str:
    """숫자는 도구 결과를 그대로 사용해 한국어 보고서를 구성한다.

    LLM이 표시용 반올림 단가로 총금액을 재계산하면 수 원 차이가 발생할 수 있다.
    따라서 Tool Calling은 LLM이 담당하되, 최종 수치 표시는 Python이 담당한다.
    """
    # 조회 결과만 요구된 경우에는 가격·날짜·거래건수만 출력한다.
    if name == "get_market_price":
        conditions = result["conditions"]
        lines = [
            "[감귤 경락가격 조회 — 검증된 도구 결과]",
            f"시장: {result['market']} | 품종: {conditions['variety']} | "
            f"등급: {conditions['grade']} | 크기: {conditions['size']} | "
            f"포장: 상자 {conditions['package_kg']}kg",
            f"최근 관측일: {result['observed_date']} | 거래: {result['transactions']:,}건",
            f"관측 경락가격: {result['observed_price_krw_per_kg']}원/kg",
            "※ 최근 관측 경락가격이며 실시간 가격이나 농가 실수취가격이 아닙니다.",
        ]
        if result.get("quality_flags"):
            lines.append("데이터 주의 표시: " + ", ".join(result["quality_flags"]))
        return "\n".join(lines)

    if name != "compare_shipping":
        raise ValueError(f"지원하지 않는 도구 결과입니다: {name}")

    conditions = result["conditions"]
    observation = result["observed_market_price"]
    lines = [
        "[감귤 출하 시점 비교 — 검증된 도구 계산 결과]",
        f"시장: {conditions['market']} | 품종: {conditions['variety']} | "
        f"등급: {conditions['grade']} | 크기: {conditions['size']} | "
        f"포장: 상자 {conditions['package_kg']}kg",
        f"입력 물량: {conditions['quantity_kg']}kg | 보관: {conditions['delay_days']}일 | "
        f"하루 보관비: {conditions['daily_storage_cost_krw']}원 | "
        f"전체 감량률: {conditions['loss_percent']}%",
        f"비교 기준가격: {result['current_price_krw_per_kg']}원/kg "
        f"({result['current_price_source']})",
    ]
    if observation.get("has_price"):
        lines.append(
            f"시장 가격 최근 관측일: {observation['observed_date']} "
            f"(거래 {observation['transactions']:,}건)"
        )
        if observation.get("quality_flags"):
            lines.append("데이터 주의 표시: " + ", ".join(observation["quality_flags"]))
    # 최근 시장 관측가격과 사용자 직접 입력가격의 출처를 반드시 구분한다.
    revenue_label = (
        "최근 관측가격 기준 즉시 출하 가정 매출"
        if result["current_price_source"] == "CSV 최근 관측값"
        else "사용자 입력가격 기준 즉시 출하 가정 매출"
    )
    lines += [
        f"{revenue_label}: {result['current_gross_krw']}원",
        f"{conditions['delay_days']}일 후 판매 가능 물량(가정): {result['remaining_kg']}kg",
        f"추가 보관비: {result['extra_storage_cost_krw']}원",
        f"지연 출하 손익분기 단가: {result['break_even_price_krw_per_kg']}원/kg",
        f"원 단위 가격 기준 최소 필요 단가: {result['minimum_integer_price_krw_per_kg']}원/kg 이상",
        f"필요 가격 상승률: {result['required_increase_percent']}%",
        "※ 총매출은 표시용 단가를 다시 곱하지 않고, 반올림 전 가격으로 계산했습니다.",
    ]
    # 사용자가 미래 가정가격을 제시한 경우에만 추가 차액이 존재한다.
    future = result.get("future_hypothesis")
    if future:
        lines += [
            "[사용자 지정 미래 가격 시나리오 — 예측 아님]",
            f"가정한 지연 후 가격: {future['assumed_price_krw_per_kg']}원/kg",
            f"보관비 차감 후 지연 출하 금액: {future['delayed_sale_minus_storage_krw']}원",
            f"즉시 출하 대비 차액: {future['difference_vs_now_krw']}원",
        ]

    # 과거 단 한 쌍의 시점 비교다. 가격 상승 가능성이나 미래 수익을 의미하지 않는다.
    past = observation.get("past_price_change", {})
    if past and past.get("change_percent") is not None:
        lines.append(
            f"관측된 이전 {conditions['delay_days']}일 가격 변화: "
            f"{past['from_date']} {past['from_price_krw_per_kg']}원/kg → "
            f"{past['to_date']} {past['to_price_krw_per_kg']}원/kg "
            f"({past['change_percent']}%)"
        )
        lines.append("※ 과거 변동은 미래 가격 예측이 아닙니다.")
    lines.extend("※ " + caution for caution in result["cautions"])
    return "\n".join(lines)


# 함수명 허용 목록(화이트리스트)과 인수 형태를 확인한 뒤에만 실제 Python 함수를 호출한다.
def execute_tool(name: str, args: dict, prices_file: Path) -> dict:
    """LLM이 지정한 도구만 허용하고, 계산 관련 오류는 결과로 전달한다."""
    allowed = {
        "get_market_price": get_market_price,
        "compare_shipping": compare_shipping,
    }
    if name not in allowed:
        return {"error": "등록되지 않은 도구입니다.", "tool_name": name}
    # 스키마에서 선언한 입력 키와 실제 전달 키를 비교해 누락·추가 인수를 거부한다.
    expected = set(PRICE_FIELDS if name == "get_market_price" else SHIPPING_FIELDS)
    if not isinstance(args, dict) or set(args) != expected:
        return {"error": "도구 입력 필드가 누락되었거나 예상하지 않은 필드가 있습니다.", "required_fields": sorted(expected)}
    try:
        return allowed[name](args, prices_file)
    # 예측 가능한 도구 오류는 전체 대화 중단 대신 오류 데이터를 LLM에 돌려준다.
    except (KeyError, OSError, ValueError, TypeError, ArithmeticError) as exc:
        return {"error": str(exc), "tool_name": name}


# 한 번의 사용자 질문에 대해 LLM의 도구 요청·실행·도구 결과 회신을 왕복한다.
def run_turn(
    client: Any,
    question: str,
    *,
    previous_response_id: str | None = None,
    model: str = "gpt-5-mini",
    prices_file: Path = DEFAULT_PRICES_FILE,
    show_tools: bool = False,
    max_rounds: int = 5,
) -> tuple[str, str]:
    """Responses API의 함수 호출을 실행하고, 호출 결과를 LLM에 다시 전달한다."""
    if not question.strip():
        raise ValueError("질문이 비어 있습니다.")
    # tool_choice="auto": 모델이 도구 호출 또는 추가 질문 중 하나를 선택한다.
    # parallel_tool_calls=False: 같은 시점에 여러 함수 호출이 뒤섞이는 상황을 피한다.
    request = {
        "model": model,
        "instructions": SYSTEM_PROMPT,
        "input": question,
        "tools": TOOLS,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    }
    # 이전 응답 ID를 전달하면 새 질문이 앞선 대화 문맥을 이어받는다.
    if previous_response_id:
        request["previous_response_id"] = previous_response_id
    response = client.responses.create(**request)
    # 도구 결과는 LLM의 설명 텍스트와 별도로 보관해 최종 숫자 출력을 보호한다.
    successful_results: list[tuple[str, dict]] = []
    had_tool_error = False

    # 모델이 도구를 반복 요청할 수 있지만 무한 반복하지 않도록 횟수를 제한한다.
    for _ in range(max_rounds):
        if response.status != "completed":
            raise RuntimeError(f"LLM 응답이 완료되지 않았습니다: {response.status}")
        # tool 호출 응답과 최종 자연어 응답은 형태가 다르다. function_call 항목만 실행한다.
        calls = [item for item in response.output if item.type == "function_call"]
        # 더 이상 호출할 함수가 없으면 한 턴이 완료된 것이다.
        if not calls:
            # LLM의 설명 문장에 포함된 숫자는 재계산·반올림 오류가 생길 수 있다.
            # 성공한 분석 결과가 있으면 모델의 숫자 설명 대신 검증된 도구
            # 결과로 보고서를 구성한다. 입력이 부족한 경우에는 LLM의 추가 질문을 쓴다.
            if successful_results and not had_tool_error:
                # 가격 조회와 출하 비교를 모두 했다면 더 완전한 compare_shipping 결과만 보고한다.
                comparisons = [item for item in successful_results if item[0] == "compare_shipping"]
                selected = comparisons or [
                    item for item in successful_results if item[0] == "get_market_price"
                ]
                return "\n\n".join(
                    format_verified_result(name, result) for name, result in selected
                ), response.id
            content = (response.output_text or "").strip()
            if not content:
                raise RuntimeError("LLM이 빈 답변을 반환했습니다.")
            return content, response.id

        # 각 function_call의 JSON 문자열을 파싱하여 이름에 맞는 Python 함수로 전달한다.
        outputs = []
        for call in calls:
            try:
                args = json.loads(call.arguments)
            except json.JSONDecodeError:
                result = {"error": "도구 입력이 유효한 JSON이 아닙니다."}
            else:
                result = execute_tool(call.name, args, prices_file)
            # 예외를 정상 분석결과로 착각해 보고서에 쓰지 않는다.
            if "error" in result:
                had_tool_error = True
            elif call.name == "compare_shipping" or (
                call.name == "get_market_price" and result.get("has_price")
            ):
                successful_results.append((call.name, result))
            if show_tools:
                print(f"[도구 호출] {call.name}", file=sys.stderr)
                if "error" in result:
                    print(f"[도구 오류] {result['error']}", file=sys.stderr)
            # call_id가 원래 모델의 요청과 실행 결과를 연결하는 식별자다.
            outputs.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
        # 완료된 응답 ID와 도구별 call_id를 이용해 동일 대화를 이어간다.
        # 도구 결과를 직전 response.id에 연결해 모델이 계속 대화하게 한다.
        response = client.responses.create(
            model=model,
            instructions=SYSTEM_PROMPT,
            input=outputs,
            previous_response_id=response.id,
            tools=TOOLS,
            tool_choice="auto",
            parallel_tool_calls=False,
        )
    raise RuntimeError("도구 호출이 반복되어 최대 실행 횟수를 초과했습니다.")


# CLI 진입점: 설정 파일 확인 → 패키지 및 CSV 확인 → 대화 반복 순서로 수행한다.
def main(argv: list[str] | None = None) -> int:
    # 프로젝트 루트의 .env를 읽는다. OS 환경변수가 설정되어 있다면 그 값을 우선한다.
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("[오류] python-dotenv가 설치되지 않았습니다. "
              "python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    # override=False이므로 잘못된 OS 환경변수가 남아 있으면 .env 수정만으로 해결되지 않는다.
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)

    parser = argparse.ArgumentParser(description="감귤 출하 판단 Tool Calling Agent")
    parser.add_argument("--question", help="질문 하나만 실행하고 종료. 없으면 대화 모드")
    parser.add_argument("--prices-file", type=Path, default=DEFAULT_PRICES_FILE)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    parser.add_argument("--show-tools", action="store_true", help="호출된 도구 이름 표시")
    args = parser.parse_args(argv)

    if not args.prices_file.is_file():
        print(f"[오류] 가격 CSV가 없습니다: {args.prices_file}", file=sys.stderr)
        return 1
    if not os.environ.get("OPENAI_API_KEY"):
        print("[오류] OPENAI_API_KEY 환경변수를 설정해 주세요.", file=sys.stderr)
        return 1
    try:
        from openai import OpenAI
    except ImportError:
        print("[오류] Python 패키지가 없습니다: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1

    # API 호출 재시도·연결 제한과 분석 도구의 최대 라운드 수는 서로 다른 개념이다.
    client = OpenAI(timeout=30, max_retries=2)
    previous_id = None
    try:
        if args.question:
            reply, _ = run_turn(client, args.question, model=args.model,
                                prices_file=args.prices_file, show_tools=args.show_tools)
            print(reply)
            return 0

        print("감귤 출하 판단 Agent (종료: exit). 정보가 부족하면 추가 질문합니다.")
        while True:
            try:
                question = input("\n질문> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if question.lower() in {"exit", "quit", "종료"}:
                break
            if not question:
                continue
            # 매 턴 반환된 응답 ID를 다음 질문에 넘겨 이전 사용자 조건을 기억하게 한다.
            reply, previous_id = run_turn(client, question,
                                          previous_response_id=previous_id,
                                          model=args.model, prices_file=args.prices_file,
                                          show_tools=args.show_tools)
            print(f"\nAgent> {reply}")
    except Exception as exc:
        # 요청 전문이나 키를 출력하지 않고, 예외 유형별로 필요한 조치만 안내한다.
        error_kind = type(exc).__name__
        messages = {
            "AuthenticationError": (
                "OpenAI 인증 실패(401): API 키가 유효한지 확인해 주세요. "
                "이미 설정된 OPENAI_API_KEY 환경변수가 .env보다 우선합니다."
            ),
            "PermissionDeniedError": "모델 또는 프로젝트 접근 권한이 없습니다(403).",
            "RateLimitError": "요청 한도 또는 계정의 API 사용 가능량을 확인해 주세요(429).",
            "APIConnectionError": "OpenAI API 연결에 실패했습니다. 네트워크를 확인해 주세요.",
            "APITimeoutError": "OpenAI API 응답 대기 시간이 초과됐습니다.",
        }
        print(f"[실행 실패] {messages.get(error_kind, error_kind)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
