"""자연어로 과거 감귤 가격 비교와 B안(보관 후 출하) 가정 단가를 조회한다."""

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import re
import sys

from .price_history import DEFAULT_FILE, FILTERS, InputError, execute_tool
from .price_scenario import execute_scenario_tool


DEFAULT_MODEL = "gpt-6-luna"
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
SCENARIO_TOOL_NAME = "get_price_scenarios"
SCENARIO_LABELS = {
    "option_a_price_per_kg": "A안 단가(원/kg)",
    "option_a_sale_date": "A안 판매일",
    "option_b_sale_date": "B안 판매일 (A안보다 며칠 뒤인지)",
}
SCENARIO_PROPERTIES = {key: PROPERTIES[key] for key in FILTERS}
SCENARIO_PROPERTIES.update(
    option_a_price_per_kg={"type": ["number", "null"],
                           "description": "A안(먼저 파는 안) 단가 원/kg. 사용자가 말한 값만"},
    option_a_days_from_today={"type": ["integer", "null"],
                              "description": "A안 판매일이 오늘부터 며칠 뒤인지. 오늘·지금=0, 내일=1. 말하지 않았으면 null"},
    option_a_sale_date={"type": ["string", "null"],
                        "description": "A안 판매일을 날짜로 말했을 때만. 연도까지 말했으면 YYYY-MM-DD, 월·일만이면 MM-DD"},
    days_after_option_a={"type": ["integer", "null"],
                         "description": "B안을 A안보다 며칠 뒤에 파는지 A안 기준으로 말했을 때. 일주일=7, 2주=14"},
    option_b_days_from_today={"type": ["integer", "null"],
                              "description": "B안 판매일을 오늘 기준으로 말했을 때 오늘부터 며칠 뒤인지"},
    option_b_sale_date={"type": ["string", "null"],
                        "description": "B안 판매일을 날짜로 말했을 때만. 연도까지 말했으면 YYYY-MM-DD, 월·일만이면 MM-DD"},
    asks_profit={"type": "boolean", "description": "손익, 얼마 더 남는지, 어느 쪽이 이득인지 물었으면 true"},
)


def _tool(name: str, description: str, properties: dict) -> dict:
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}


TOOL = _tool(TOOL_NAME, "동일 규격의 과거 거래 가격 비교 조회", PROPERTIES)
SCENARIO_TOOL = _tool(SCENARIO_TOOL_NAME,
                      "지금 출하(A안)와 며칠 뒤 출하(B안) 비교용 B안 가정 단가 조회", SCENARIO_PROPERTIES)
TOOLS = [TOOL, SCENARIO_TOOL]
SCHEMAS = {TOOL_NAME: PROPERTIES, SCENARIO_TOOL_NAME: SCENARIO_PROPERTIES}
INSTRUCTIONS = """사용자 질문을 보고 두 도구 중 하나를 호출해 조건만 추출한다. 숫자 계산과 답변 문장은 프로그램이 만든다.
get_price_history: 과거 며칠 사이 가격이 어떻게 변했는지 묻는 질문. 예: 조생귤 7일 가격 어땠어?
get_price_scenarios: 먼저 파는 안(A안)과 며칠 뒤 파는 안(B안)을 비교하거나, 보관했다가 며칠 뒤 팔면 단가가 어떻게 되는지 묻는 질문. 예: A안을 2,500원으로 가정하면 7일 뒤 B안은?
손익, 얼마 더 남는지, 어느 쪽이 이득인지 물으면 get_price_scenarios를 쓰고 asks_profit을 true로 둔다.
가격 전망·예측(오를까? 얼마가 될까?)이나 감귤 가격과 무관한 주제는 get_price_history를 intent=unsupported로 호출한다.
과거 비교 요청이나 직전 확인 질문에 대한 답이면 get_price_history의 intent는 history다.
후속 질문에서 일부 조건만 바꾸면 사용자가 바꾸지 않은 이전 조건(규격, A안 단가, 판매 시점)은 그대로 다시 넣고 바꾼 값만 새로 넣는다.
날짜는 계산하지 않는다. 빼기·더하기 없이 사용자가 말한 숫자만 그 기준의 필드에 넣는다.
A안: 오늘·지금은 option_a_days_from_today=0, 내일은 1, 3일 뒤는 3이다.
B안: A안보다 N일 뒤라고 하거나, 지금 팔지 않고 N일 뒤에 판다고 하면 days_after_option_a=N이다.
A안과 B안을 둘 다 오늘 기준으로 말하면(예: 3일 뒤에 파는 것과 10일 뒤에 파는 것) option_a_days_from_today=3, option_b_days_from_today=10이다.
대화에서 사용자가 말한 조건만 채우고 나머지는 null로 둔다.
조생 감귤은 조생귤, 극조생은 극조생감귤로 표준화할 수 있다.
비교 간격을 지정하면 해당 숫자를 그대로 전달한다.
예시, 사용자에게 제시한 후보, 이전 조회의 자동 적용 조건은 사용자 선택으로 간주하지 않는다.
이전 답변의 '자동 적용 기준' 줄에 나온 시장·거래방식은 프로그램이 넣은 값이다. 사용자가 직접 말하지 않았으면 market_code와 trade_type은 null로 둔다.
"""
UNSUPPORTED = ("과거 감귤 거래 가격 비교와, A안 단가를 정하면 며칠 뒤 출하(B안)의 가정 단가를 보여 드릴 수 있어요. "
               "예: 조생귤 7일 가격 변화, A안 2,500원이면 7일 뒤 B안은? 가격 전망(예측)은 하지 않습니다.")
PROFIT_NOTE = ("수수료·보관비·감모를 반영한 손익(얼마 더 남는지)은 팀 손익 기능과 연결되면 계산됩니다. "
               "지금은 B안 가정 단가까지만 보여 드립니다.")
DATE_TEXT = re.compile(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})")


def kst_today() -> date:
    # 한국은 서머타임이 없어 고정 +9시간을 쓴다. Windows에 시간대 데이터가 없어도 동작한다.
    return datetime.now(timezone(timedelta(hours=9))).date()


def _error_text(result: dict) -> str:
    if result.get("error_kind") == "INVALID_INPUT":
        return result["error"]
    return "조회하지 못했습니다. 입력 조건과 비교 데이터 파일을 확인해 주세요."


def _condition_notes(result: dict) -> list[str]:
    lines = []
    changes = result.get("normalized_conditions", {})
    if changes:
        labels = [
            f"{FIELD_LABELS[field]} {change['from']} → {change['to'] or '조건 확인 필요'}"
            for field, change in changes.items()
        ]
        lines.append("자료 표기로 바꾼 조건 — " + ", ".join(labels))
    if result.get("applied_defaults_note"):
        lines.append("자동 적용 기준(사용자 지정 아님, 자료에 하나뿐인 값) — " + result["applied_defaults_note"])
    return lines


def _ask_fields(result: dict) -> str:
    fields = []
    for key in result["missing_fields"]:
        options = result.get("choices", {}).get(key, [])
        example = f" (예: {', '.join(map(str, options[:3]))})" if options else ""
        fields.append({**FIELD_LABELS, **SCENARIO_LABELS}[key] + example)
    return "다음 조건을 알려 주세요. " + ", ".join(fields)


def _spec_line(conditions: dict) -> str:
    return (f"{conditions['variety']} · {conditions['grade']} · {conditions['size']} · "
            f"{conditions['package']} {Decimal(conditions['package_kg']):g}kg")


def format_result(result: dict) -> str:
    status = result["status"]
    if status == "ERROR":
        return _error_text(result)
    lines = _condition_notes(result)
    if status == "NEEDS_CLARIFICATION":
        return "\n".join(lines + [_ask_fields(result)])
    if status == "INTERVAL_NOT_AVAILABLE":
        intervals = ", ".join(f"{n}일" for n in result["available_intervals"]) or "없음"
        return "\n".join(lines + [result["message"], f"현재 자료의 비교 간격은 {intervals}입니다."])
    if status not in {"OK", "NO_CLEAN_COMPARISON"}:
        return "\n".join(lines + [result["message"]])

    lines.append(f"{_spec_line(result['conditions'])} · {result['interval_days']}일 간격")
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


def format_scenario(result: dict, *, notes=(), asks_profit=False) -> str:
    status = result["status"]
    if status == "ERROR":
        return _error_text(result)
    lines = [*notes, *_condition_notes(result)]
    tail = [PROFIT_NOTE] if asks_profit else []
    if status == "NEEDS_CLARIFICATION" and "scenarios" not in result:
        return "\n".join(lines + [_ask_fields(result)] + tail)
    if status == "INTERVAL_NOT_AVAILABLE" or "scenarios" not in result:
        # 지원하지 않는 간격이나 자료 없음이면 이전 턴의 가정 단가를 다시 보여 주지 않는다.
        return "\n".join(lines + [result["message"]] + tail)

    a, b, interval = result["option_a"], result["option_b"], result["interval_days"]
    a_price = a["price_per_kg"]
    lines.append(_spec_line(result["conditions"]))
    lines.append(f"A안 {a['sale_date']} 판매 · "
                 + (f"{Decimal(a_price):,.2f}원/kg (사용자 입력 가정)" if a_price else "단가 미입력"))
    lines.append(f"B안 {b['sale_date']} 판매 · A안보다 {interval}일 뒤")
    start, end = result["base_date_range"]
    lines.append(f"동일 규격 {interval}일 과거 사례 {result['clean_pairs']}쌍 (기준일 {start} ~ {end})")
    for item in result["scenarios"]:
        price = item["option_b_price_per_kg"]
        shown = f" → B안 {Decimal(price):,.2f}원/kg [가정]" if price else ""
        lines.append(f"- {item['label']} {Decimal(item['change_pct']):+.2f}%{shown}")
    if status == "NEEDS_CLARIFICATION":
        lines.append(result["message"])
    lines.extend(f"※ {text}" for text in result["cautions"])
    lines.extend(tail)
    lines.append(f"출처 {result['source']['comparison_file']}")
    return "\n".join(lines)


def _parse_day(text, today: date, label: str) -> date:
    match = DATE_TEXT.fullmatch(text.strip()) if isinstance(text, str) else None
    if not match:
        raise InputError(f"{label}은 YYYY-MM-DD 또는 MM-DD 형식이어야 합니다.")
    year, month, day_ = match.groups()
    try:
        if year:
            return date(int(year), int(month), int(day_))
        # 연도 없이 월·일만 말하면 출하 계획이므로 오늘 이후 가장 가까운 날짜로 본다.
        guess = date(today.year, int(month), int(day_))
        return guess if guess >= today else date(today.year + 1, int(month), int(day_))
    except ValueError as exc:
        raise InputError(f"{label}이 실제로 없는 날짜입니다.") from exc


def _days(value, label: str, lowest: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not lowest <= value <= 366:
        raise InputError(f"{label}은 {lowest}~366 사이 정수여야 합니다.")
    return value


def resolve_sale_dates(args: dict, today: date) -> tuple[date, date | None, list[str]]:
    """모델이 말 그대로 옮긴 시점 표현을 실제 날짜로 바꾼다. 날짜 계산은 모델에 맡기지 않는다."""
    notes = []
    offset = _days(args.get("option_a_days_from_today"), "A안 판매 시점(오늘부터 며칠 뒤)", 0)
    a_text = args.get("option_a_sale_date")
    candidates = {today + timedelta(days=offset)} if offset is not None else set()
    if a_text is not None:
        candidates.add(_parse_day(a_text, today, "A안 판매일"))
    if len(candidates) > 1:
        raise InputError("A안 판매일이 두 가지로 지정됐습니다. 하나만 알려 주세요.")
    if not candidates:
        # A안은 '지금 출하'가 기본이라 판매일을 말하지 않으면 오늘로 두고 화면에 밝힌다.
        candidates = {today}
        notes.append(f"A안 판매일을 따로 말하지 않아 오늘({today.isoformat()})로 두었습니다.")
    a_day = candidates.pop()
    gap = _days(args.get("days_after_option_a"), "B안 판매 시점(A안보다 며칠 뒤)", 1)
    b_offset = _days(args.get("option_b_days_from_today"), "B안 판매 시점(오늘부터 며칠 뒤)", 1)
    b_text = args.get("option_b_sale_date")
    # 기준(A안·오늘·날짜)별로 각각 날짜를 만든 뒤 비교한다. 간격은 두 판매일의 차이로만 정해진다.
    candidates = {a_day + timedelta(days=gap)} if gap is not None else set()
    if b_offset is not None:
        candidates.add(today + timedelta(days=b_offset))
    if b_text is not None:
        candidates.add(_parse_day(b_text, today, "B안 판매일"))
    if len(candidates) > 1:
        raise InputError("B안 판매일이 두 가지로 지정됐습니다. A안보다 며칠 뒤인지 하나만 알려 주세요.")
    return a_day, (candidates.pop() if candidates else None), notes


def answer_scenario(args: dict, *, today: date, data_file=DEFAULT_FILE) -> str:
    asks_profit = args.get("asks_profit") is True
    try:
        a_day, b_day, notes = resolve_sale_dates(args, today)
    except InputError as exc:
        return str(exc)
    request = {key: args[key] for key in FILTERS if args.get(key) is not None}
    request["option_a_sale_date"] = a_day.isoformat()
    if b_day is not None:
        request["option_b_sale_date"] = b_day.isoformat()
    if args.get("option_a_price_per_kg") is not None:
        request["option_a_price_per_kg"] = args["option_a_price_per_kg"]
    # 매 턴 새로 계산한다. 이전 턴 결과를 재사용하지 않아 조건을 바꾸면 이전 가격이 남지 않는다.
    result = execute_scenario_tool(request, data_file=data_file)
    return format_scenario(result, notes=notes, asks_profit=asks_profit)


def run_turn(client, messages, *, model, data_file=DEFAULT_FILE, today=None):
    response = client.responses.create(
        model=model, instructions=INSTRUCTIONS, input=messages, tools=TOOLS,
        tool_choice="required", parallel_tool_calls=False, store=False,
    )
    calls = [item for item in response.output if item.type == "function_call"]
    if response.status != "completed" or len(calls) != 1 or calls[0].name not in SCHEMAS:
        return "질문의 조회 조건을 읽지 못했습니다. 품종과 비교 간격을 포함해 다시 질문해 주세요."
    name = calls[0].name
    try:
        args = json.loads(calls[0].arguments)
    except json.JSONDecodeError:
        return "질문의 조회 조건을 읽지 못했습니다. 다시 질문해 주세요."
    if not isinstance(args, dict) or set(args) != set(SCHEMAS[name]):
        return "질문의 조회 조건을 읽지 못했습니다. 다시 질문해 주세요."
    # 모델은 조건만 추출하고, 표시할 수치와 안내 문구는 조회 결과로 만든다.
    if name == SCENARIO_TOOL_NAME:
        return answer_scenario(args, today=today or kst_today(), data_file=data_file)
    if args.pop("intent") != "history":
        return UNSUPPORTED
    return format_result(execute_tool(TOOL_NAME, {**args, "limit": 3}, data_file))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--question", help="질문 하나를 실행하고 종료")
    modes.add_argument("--demo", action="store_true", help="API 없이 예시 조건을 조회")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL") or DEFAULT_MODEL)
    args = parser.parse_args()
    if args.demo:
        spec = dict(variety="조생귤", grade="특", size="소", package="상자", package_kg=5, interval_days=7)
        queries = [{"variety": "조생귤", "interval_days": 7}, spec, {**spec, "base_date": "2026-11-03"}]
        for title, query in zip(("조건 확인", "과거 사례", "비교일 자료 부족"), queries):
            print(f"\n[{title}]\n{format_result(execute_tool(TOOL_NAME, {**query, 'limit': 3}))}")
        plan = dict.fromkeys(SCENARIO_PROPERTIES)
        plan.update({k: spec[k] for k in FILTERS if k in spec}, option_a_days_from_today=0,
                    option_a_price_per_kg=2500, days_after_option_a=7, asks_profit=False)
        demos = (("B안 가정 단가 (A안 2,500원 · 7일 뒤)", plan),
                 ("B안만 14일 뒤로", {**plan, "days_after_option_a": 14}),
                 ("지원하지 않는 간격 (10일 뒤)", {**plan, "days_after_option_a": 10}))
        for title, query in demos:
            print(f"\n[{title}]\n{answer_scenario(query, today=kst_today())}")
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
