"""자연어로 과거 감귤 가격 비교를 조회한다."""

import argparse
from decimal import Decimal
import json
import os
import sys

from .price_history import DEFAULT_FILE, execute_tool


TOOL_NAME = "get_price_history"
FIELD_LABELS = {
    "market_code": "시장", "variety": "품종", "grade": "등급", "size": "크기",
    "package": "포장 종류", "package_kg": "포장 중량(kg)",
    "trade_type": "거래 방식", "interval_days": "비교 간격(일)",
}
PROPERTIES = {
    key: {"type": ["string", "null"], "description": label}
    for key, label in FIELD_LABELS.items()
}
PROPERTIES.update(
    market_code={"type": ["string", "null"], "description": "서울가락을 명시하면 110001. 미지정이면 null"},
    package_kg={"type": ["number", "null"], "description": "포장 한 개의 kg. 전체 출하량과 구분"},
    interval_days={"type": ["integer", "null"], "description": "명시된 달력일 간격. 일주일은 7일"},
    base_date={"type": ["string", "null"], "description": "연도까지 확실한 과거 기준일 YYYY-MM-DD"},
    intent={"type": "string", "enum": ["history", "unsupported"]},
)
TOOL = {
    "type": "function", "name": TOOL_NAME, "description": "동일 규격의 과거 거래 가격 비교 조회",
    "strict": True,
    "parameters": {
        "type": "object", "properties": PROPERTIES,
        "required": list(PROPERTIES), "additionalProperties": False,
    },
}
INSTRUCTIONS = """사용자 질문에서 과거 감귤 가격 비교 조건을 추출한다.
과거 비교 요청이나 직전 확인 질문에 대한 답이면 intent는 history다.
미래 가격, 지금 출하 여부, 손익 계산, 그 밖의 주제면 unsupported다.
대화에서 사용자가 말한 조건만 채우고 나머지는 null로 둔다.
조생 감귤은 조생귤, 극조생은 극조생감귤로 표준화할 수 있다.
비교 간격을 지정하면 해당 숫자를 그대로 전달한다.
예시, 사용자에게 제시한 후보, 이전 조회의 자동 적용 조건은 사용자 선택으로 간주하지 않는다.
"""


def format_result(result: dict) -> str:
    status = result["status"]
    if status == "ERROR":
        if result.get("error_kind") == "INVALID_INPUT":
            return result["error"]
        return "조회하지 못했습니다. 입력 조건과 비교 데이터 파일을 확인해 주세요."
    lines = []
    changes = result.get("normalized_conditions", {})
    if changes:
        labels = [
            f"{FIELD_LABELS[field]} {change['from']} → {change['to'] or '조건 확인 필요'}"
            for field, change in changes.items()
        ]
        lines.append("자료 표기로 바꾼 조건 — " + ", ".join(labels))
    if result.get("applied_defaults_note"):
        lines.append("자료에서 자동 적용한 기준 — " + result["applied_defaults_note"])
    if status == "NEEDS_CLARIFICATION":
        fields = []
        for key in result["missing_fields"]:
            options = result["choices"].get(key, [])
            example = f" (예: {', '.join(map(str, options[:3]))})" if options else ""
            fields.append(FIELD_LABELS[key] + example)
        return "\n".join(lines + ["다음 조건을 알려 주세요. " + ", ".join(fields)])
    if status == "INTERVAL_NOT_AVAILABLE":
        intervals = ", ".join(f"{n}일" for n in result["available_intervals"]) or "없음"
        return "\n".join(lines + [result["message"], f"현재 자료의 비교 간격은 {intervals}입니다."])
    if status not in {"OK", "NO_CLEAN_COMPARISON"}:
        return "\n".join(lines + [result["message"]])

    conditions = result["conditions"]
    lines.append(
        f"{conditions['variety']} · {conditions['grade']} · {conditions['size']} · "
        f"{conditions['package']} {Decimal(conditions['package_kg']):g}kg · "
        f"{result['interval_days']}일 간격"
    )
    lines.append(
        f"경고 없음 {result['clean_pairs']}쌍 / 경고 있음 {result['warning_pairs']}쌍 / "
        f"비교 불가 {result['unavailable_pairs']}건"
    )
    changes = result["clean_change_range_pct"]
    if changes:
        lines.append(f"경고 없는 과거 변동 범위 {Decimal(changes['min']):+.2f}% ~ {Decimal(changes['max']):+.2f}%")
        lines.append(f"최근 기준일 사례 {result['displayed_examples']}개")
        for row in result["examples"]:
            lines.append(
                f"- {row['base_date']} → {row['target_date']} · "
                f"{Decimal(row['base_price_kg']):,.2f} → {Decimal(row['target_price_kg']):,.2f}원/kg "
                f"({Decimal(row['price_change_pct']):+.2f}%)"
            )
    else:
        lines.append("이 조건에서 경고 없이 비교할 수 있는 사례가 없습니다.")
    warning_labels = {"LOW_SAMPLE": "거래 건수 적음", "GRADE_UNKNOWN": "등급 미상",
                      "SIZE_UNKNOWN": "크기 미상", "KEY_FIELD_UNKNOWN": "일부 조건 미상",
                      "QUALITY_FLAG_MISSING": "품질 표시 누락", "LABEL_MISMATCH": "표시명 불일치"}
    for code, count in result["warning_counts"].items():
        lines.append(f"- 품질 경고 {warning_labels.get(code, code)} {count}건")
    for reason in result["unavailable_reasons"]:
        lines.append(f"- {reason['message']} ({reason['count']}건)")
    lines.append("과거 경락가격을 비교했습니다. 날짜가 겹치는 사례가 있어 미래 확률로 해석할 수 없습니다.")
    lines.append(f"출처 {result['source']}")
    return "\n".join(lines)


def run_turn(client, messages, *, model, data_file=DEFAULT_FILE):
    response = client.responses.create(
        model=model, instructions=INSTRUCTIONS, input=messages, tools=[TOOL],
        tool_choice={"type": "function", "name": TOOL_NAME}, parallel_tool_calls=False,
        store=False,
    )
    calls = [item for item in response.output if item.type == "function_call"]
    if response.status != "completed" or len(calls) != 1 or calls[0].name != TOOL_NAME:
        return "질문의 조회 조건을 읽지 못했습니다. 품종과 비교 간격을 포함해 다시 질문해 주세요."
    try:
        args = json.loads(calls[0].arguments)
    except json.JSONDecodeError:
        return "질문의 조회 조건을 읽지 못했습니다. 다시 질문해 주세요."
    if not isinstance(args, dict) or set(args) != set(PROPERTIES):
        return "질문의 조회 조건을 읽지 못했습니다. 다시 질문해 주세요."
    if args.pop("intent") != "history":
        return "과거 감귤 거래 가격을 비교할 수 있어요. 예를 들어 조생귤의 7일 가격 변화를 물어보세요."
    # 모델은 조건만 추출하고, 표시할 수치와 안내 문구는 조회 결과로 만든다.
    return format_result(execute_tool(TOOL_NAME, {**args, "limit": 3}, data_file))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--question", help="질문 하나를 실행하고 종료")
    modes.add_argument("--demo", action="store_true", help="API 없이 예시 조건 세 가지를 조회")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    args = parser.parse_args()
    if args.demo:
        spec = dict(variety="조생귤", grade="특", size="소", package="상자", package_kg=5, interval_days=7)
        queries = [{"variety": "조생귤", "interval_days": 7}, spec, {**spec, "base_date": "2026-11-03"}]
        for title, query in zip(("조건 확인", "과거 사례", "비교일 자료 부족"), queries):
            print(f"\n[{title}]\n{format_result(execute_tool(TOOL_NAME, {**query, 'limit': 3}))}")
        return 0
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY를 설정하거나 --demo로 실행해 주세요.", file=sys.stderr)
        return 1
    try:
        from openai import OpenAI, OpenAIError
    except ImportError:
        print("python -m pip install openai 명령으로 SDK를 설치해 주세요.", file=sys.stderr)
        return 1
    client = OpenAI(timeout=30, max_retries=1)
    messages = []
    while True:
        try:
            question = args.question if args.question is not None else input("질문> ")
        except (EOFError, KeyboardInterrupt):
            return 0
        if question.strip().lower() in {"종료", "exit", "quit"}:
            return 0
        if not question.strip():
            if args.question is not None:
                parser.error("질문이 비어 있습니다.")
            continue
        messages.append({"role": "user", "content": question})
        try:
            reply = run_turn(client, messages, model=args.model)
        except OpenAIError as exc:
            print(f"API 호출 실패 ({type(exc).__name__}). 키, 모델 접근 권한, 연결을 확인해 주세요.", file=sys.stderr)
            return 1
        print(reply)
        messages.append({"role": "assistant", "content": reply})
        if args.question is not None:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
