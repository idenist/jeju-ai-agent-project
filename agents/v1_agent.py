#!/usr/bin/env python3
"""제주 감귤 출하 V1 Agent: V0를 수정하지 않고 과거 손익 분석 도구를 확장한다.

V0의 가격 조회·손익분기 함수와 보고서는 재사용한다. 신규 도구의 규격,
실행·보고 흐름만 여기서 조립하므로 V0 고정 평가셋의 파일 버전은 유지된다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.agent import (  # noqa: E402
    SYSTEM_PROMPT as V0_SYSTEM_PROMPT,
    TOOLS as V0_TOOLS,
    PRICE_FIELDS,
    tool_schema,
    execute_tool as execute_v0_tool,
    format_verified_result as format_v0_result,
)
from agents.baseline import DEFAULT_PRICES_FILE  # noqa: E402
from agents.historical_analysis import (  # noqa: E402
    analyze_historical_delays,
    format_historical_report,
)

# V1의 도구와 계산은 기존 V0 모듈과 분리한다. 같은 물량·비용이라도
# V0는 최신 가격 한 점의 손익분기, V1은 여러 과거 날짜쌍의 가상 손익이다.
SYSTEM_PROMPT = V0_SYSTEM_PROMPT + """

V1에서 새로 지원하는 분석:
- 과거에 N일 보관했다고 가정했을 때 각 날짜쌍의 가상 손익 분석을 원하면
  반드시 analyze_historical_delays 도구를 사용하세요.
- 과거 분석의 거래건수·관측일·표본·데이터 한계만 점검하길 원하면
  assess_evidence_quality를 사용하세요.
- 두 도구 모두 실물 감귤의 실제 보관 기록이나 미래 가격·확률 예측이 아닙니다.
- 과거 이익 구간의 비율을 미래 이익 확률로 설명하거나 확정적으로 출하를 권하지 마세요.
- 기존 가격 조회·손익분기 질문이면 V0의 기존 도구를 그대로 사용하세요.
- 과거 분석에는 품종·등급·크기·포장중량과 물량·지연기간·하루 보관비·
  해당 지연기간의 전체 누적 감량률이 필요합니다. 없으면 지어내지 말고 물어보세요.
- '미상'은 원자료 결측 등급이지, 사용자가 등급을 모를 때 임의로 채우는 값이 아닙니다.
- 서울가락시장만 분석합니다. 최신성 기준일은 Python 실행 환경에서 주입합니다.
  as_of_date는 지정하지 않았다면 null, min_transactions_per_day는 null(기본 5건)입니다.
"""

# OpenAI strict Function Calling 규칙에 맞춰 모든 인수 키가 필요하며,
# 시스템 기본값을 사용하는 선택 항목은 null로 보내도록 선언한다.
HISTORICAL_FIELDS = {
    **PRICE_FIELDS,
    "quantity_kg": {"type": "number", "description": "출하 물량(kg). 3톤은 3000"},
    "delay_days": {"type": "integer", "description": "과거 비교할 달력상 지연일수(1 이상)"},
    "daily_storage_cost_krw": {"type": "number", "description": "하루 추가 보관비(원)"},
    "loss_percent": {"type": "number", "description": "해당 지연기간 전체 누적 감량·폐기율(%)"},
    "as_of_date": {"type": ["string", "null"], "description": "사용자 지정 관측 상한일 YYYY-MM-DD. 없다면 null"},
    "min_transactions_per_day": {"type": ["integer", "null"], "description": "사용자 지정한 날짜별 최소 거래건수. 없으면 null(기본 5건)"},
}

TOOLS = [
    *V0_TOOLS,
    tool_schema(
        "analyze_historical_delays",
        "동일 시장·품종·등급·크기·포장중량의 과거 N일 날짜쌍에 물량·보관비·감량을 대입해 가상 손익 분포와 근거 품질을 분석",
        HISTORICAL_FIELDS,
        list(HISTORICAL_FIELDS),
    ),
    tool_schema(
        "assess_evidence_quality",
        "과거 지연 출하 시뮬레이션의 표본 수·저표본 제외·최신성·분석 한계를 점검. 미래 확률과 실제 수익 예측은 금지",
        HISTORICAL_FIELDS,
        list(HISTORICAL_FIELDS),
    ),
]


def execute_tool(name: str, args: dict, prices_file: Path, *, analysis_date: date | None = None) -> dict:
    """V0 도구는 원본 실행기로, V1 도구는 새 분석기로 보내는 화이트리스트."""
    if name in {"get_market_price", "compare_shipping"}:
        return execute_v0_tool(name, args, prices_file)
    if name not in {"analyze_historical_delays", "assess_evidence_quality"}:
        return {"error": "등록되지 않은 도구입니다.", "tool_name": name}
    if not isinstance(args, dict) or set(args) != set(HISTORICAL_FIELDS):
        return {"error": "도구 입력 필드가 누락되었거나 예상하지 않은 필드가 있습니다.",
                "required_fields": sorted(HISTORICAL_FIELDS)}
    try:
        result = analyze_historical_delays(args, prices_file, analysis_date=analysis_date)
    except (KeyError, OSError, ValueError, TypeError, ArithmeticError) as exc:
        return {"error": str(exc), "tool_name": name}
    if name == "assess_evidence_quality":
        result.pop("pairs")  # 품질 점검만 원하는 질문에서 전체 날짜쌍 전송을 생략한다.
    return result


def format_verified_result(name: str, result: dict) -> str:
    """LLM이 보고서 안의 숫자를 재생성하지 않도록 Python 도구 결과만 표시한다."""
    if name in {"analyze_historical_delays", "assess_evidence_quality"}:
        return format_historical_report(result, quality_only=name == "assess_evidence_quality")
    return format_v0_result(name, result)


def run_turn(
    client: Any,
    question: str,
    *,
    previous_response_id: str | None = None,
    model: str = "gpt-5-mini",
    prices_file: Path = DEFAULT_PRICES_FILE,
    show_tools: bool = False,
    max_rounds: int = 5,
    analysis_date: date | None = None,
) -> tuple[str, str]:
    """LLM에게 V0+V1 도구 4종을 알리고 함수 호출 → 실행 → 결과 회신한다."""
    if not question.strip():
        raise ValueError("질문이 비어 있습니다.")
    request = {
        "model": model, "instructions": SYSTEM_PROMPT, "input": question,
        "tools": TOOLS, "tool_choice": "auto", "parallel_tool_calls": False,
    }
    if previous_response_id:
        request["previous_response_id"] = previous_response_id
    response = client.responses.create(**request)
    success: list[tuple[str, dict]] = []
    error_occurred = False

    for _ in range(max_rounds):
        if response.status != "completed":
            raise RuntimeError(f"LLM 응답이 완료되지 않았습니다: {response.status}")
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            if success and not error_occurred:
                # 같은 질문에서 비교와 과거 분석을 모두 했다면 양쪽을 보고한다.
                # 품질 결과는 과거 분석의 경고와 겹치므로 중복 표시하지 않는다.
                historical = [x for x in success if x[0] == "analyze_historical_delays"]
                quality = [x for x in success if x[0] == "assess_evidence_quality"]
                compare = [x for x in success if x[0] == "compare_shipping"]
                if historical:
                    selected = compare + historical
                else:
                    selected = quality or compare or [x for x in success if x[0] == "get_market_price"]
                return "\n\n".join(format_verified_result(name, value) for name, value in selected), response.id
            content = (response.output_text or "").strip()
            if not content:
                raise RuntimeError("LLM이 빈 답변을 반환했습니다.")
            return content, response.id
        outputs = []
        for call in calls:
            try:
                args = json.loads(call.arguments)
            except json.JSONDecodeError:
                result = {"error": "도구 입력이 유효한 JSON이 아닙니다."}
            else:
                result = execute_tool(call.name, args, prices_file, analysis_date=analysis_date)
            if "error" in result:
                error_occurred = True
            elif call.name in {"compare_shipping", "analyze_historical_delays", "assess_evidence_quality"} or (
                call.name == "get_market_price" and result.get("has_price")
            ):
                success.append((call.name, result))
            if show_tools:
                print(f"[도구 호출] {call.name}", file=sys.stderr)
                if "error" in result:
                    print(f"[도구 오류] {result['error']}", file=sys.stderr)
            outputs.append({
                "type": "function_call_output", "call_id": call.call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
        # 도구 실행 결과는 같은 call_id, 응답 흐름은 직전 response.id로 연결한다.
        response = client.responses.create(
            model=model, instructions=SYSTEM_PROMPT, input=outputs,
            previous_response_id=response.id, tools=TOOLS,
            tool_choice="auto", parallel_tool_calls=False,
        )
    raise RuntimeError("도구 호출이 반복되어 최대 실행 횟수를 초과했습니다.")


def main(argv: list[str] | None = None) -> int:
    """V0와 동일한 .env/CLI 실행 흐름을 사용하되 V1 추가 도구를 활성화한다."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("[오류] python-dotenv가 없습니다. python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)

    parser = argparse.ArgumentParser(description="제주 감귤 출하 시점 V1 Agent")
    parser.add_argument("--question", help="질문 하나만 처리하고 종료")
    parser.add_argument("--prices-file", type=Path, default=DEFAULT_PRICES_FILE)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    parser.add_argument("--show-tools", action="store_true", help="호출된 도구 이름 표시")
    parser.add_argument("--analysis-date", help="평가 재현용 기준일 YYYY-MM-DD. 실제 분석은 생략 시 실행일 사용")
    args = parser.parse_args(argv)
    try:
        chosen_date = date.fromisoformat(args.analysis_date) if args.analysis_date else None
    except ValueError:
        parser.error("--analysis-date는 YYYY-MM-DD 형식이어야 합니다.")
    if not args.prices_file.is_file():
        print(f"[오류] 가격 CSV를 찾을 수 없습니다: {args.prices_file}", file=sys.stderr)
        return 1
    if not os.getenv("OPENAI_API_KEY"):
        print("[오류] .env에 OPENAI_API_KEY를 설정해 주세요.", file=sys.stderr)
        return 1
    try:
        from openai import OpenAI
    except ImportError:
        print("[오류] OpenAI SDK가 없습니다. python -m pip install -r requirements.txt", file=sys.stderr)
        return 1

    client = OpenAI(timeout=30, max_retries=2)
    previous_id = None
    try:
        if args.question:
            answer, _ = run_turn(
                client, args.question, model=args.model, prices_file=args.prices_file,
                show_tools=args.show_tools, analysis_date=chosen_date,
            )
            print(answer)
            return 0
        print("감귤 출하 시점 V1 Agent (종료: exit). 과거 구간 가상 손익도 분석합니다.")
        while True:
            try:
                question = input("\n질문> ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if question.lower() in {"exit", "quit", "종료"}:
                break
            if not question:
                continue
            answer, previous_id = run_turn(
                client, question, previous_response_id=previous_id, model=args.model,
                prices_file=args.prices_file, show_tools=args.show_tools,
                analysis_date=chosen_date,
            )
            print(f"\nAgent> {answer}")
    except Exception as exc:
        # 원본 오류 문자열에는 키가 포함될 수 있으므로 예외 종류만 안내한다.
        messages = {
            "AuthenticationError": "OpenAI API 키 인증 실패(401). .env와 기존 OS 환경변수를 확인하세요.",
            "PermissionDeniedError": "모델 또는 API 프로젝트 접근 권한 오류(403).",
            "RateLimitError": "API 요청 한도 또는 잔여 사용량 오류(429).",
            "APIConnectionError": "OpenAI API 연결에 실패했습니다.",
            "APITimeoutError": "OpenAI API 응답 시간이 초과됐습니다.",
        }
        print(f"[실행 실패] {messages.get(type(exc).__name__, type(exc).__name__)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
