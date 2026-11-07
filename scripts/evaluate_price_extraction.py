"""과거 가격 에이전트가 모델에서 받은 조건을 평가한다."""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

from agents.jeonghyeon.agent import PROPERTIES, TOOL_NAME, run_turn


CASE_FILE = Path(__file__).with_name("price_extraction_cases.json")


class Recorder:
    def __init__(self, client):
        self.client = client
        self.responses = self
        self.arguments = None

    def create(self, **kwargs):
        response = self.client.responses.create(**kwargs)
        calls = [item for item in response.output if item.type == "function_call"]
        if response.status == "completed" and len(calls) == 1 and calls[0].name == TOOL_NAME:
            try:
                args = json.loads(calls[0].arguments)
            except json.JSONDecodeError:
                args = None
            if isinstance(args, dict) and set(args) == set(PROPERTIES):
                self.arguments = args
        return response


def score(expected, actual, previous):
    if actual is None:
        return {"valid_call": False, "exact_match": False, "differences": {},
                "invented_fields": [], "missed_changes": [], "dropped_fields": []}
    # 지원 밖 요청에서는 에이전트가 조회를 실행하지 않으므로 의도만 평가한다.
    fields = ("intent",) if expected["intent"] == "unsupported" else PROPERTIES
    differences = {
        k: {"expected": expected[k], "actual": actual[k]}
        for k in fields if actual[k] != expected[k]
        or (type(actual[k]) is bool and type(expected[k]) is not bool)
    }
    return {
        "valid_call": True,
        "exact_match": not differences,
        "differences": differences,
        "invented_fields": [k for k in differences if expected[k] is None and actual[k] is not None],
        "missed_changes": [
            k for k in differences if k != "intent" and previous is not None
            and previous[k] is not None and previous[k] != expected[k] and expected[k] is not None
        ],
        # 사용자가 바꾸지 않은 이전 조건을 모델이 비우거나 다른 값으로 바꾼 경우다.
        "dropped_fields": [
            k for k in differences if k != "intent" and previous is not None
            and previous[k] is not None and previous[k] == expected[k]
        ],
    }


def evaluate(cases, client, model, dry_run=False):
    results = []
    for case in cases:
        messages = []
        previous = None
        for index, turn in enumerate(case["turns"], 1):
            unknown = set(turn["expected"]) - set(PROPERTIES)
            if unknown:
                raise ValueError(f"평가셋에 등록되지 않은 필드가 있습니다: {sorted(unknown)}")
            expected = dict.fromkeys(PROPERTIES)
            expected.update(turn["expected"])
            if expected["intent"] not in {"history", "unsupported"}:
                raise ValueError("평가셋에 조회 의도가 없습니다.")
            messages.append({"role": "user", "content": turn["question"]})
            recorder = Recorder(fixture_client(expected) if dry_run else client)
            reply = run_turn(recorder, messages, model=model)
            results.append({
                "case": case["id"], "turn": index, "question": turn["question"],
                "expected": expected, "actual": recorder.arguments,
                **score(expected, recorder.arguments, previous),
            })
            # 매 사례는 새 대화로 시작하고, 후속 턴에는 실제 출력한 답변을 넣는다.
            messages.append({"role": "assistant", "content": reply})
            previous = expected
    return {
        "mode": "fixture" if dry_run else "live",
        "model": None if dry_run else model,
        "turns": len(results),
        "exact_matches": sum(r["exact_match"] for r in results),
        "invalid_calls": sum(not r["valid_call"] for r in results),
        "turns_with_invented_fields": sum(bool(r["invented_fields"]) for r in results),
        "invented_fields": sum(len(r["invented_fields"]) for r in results),
        "turns_with_missed_changes": sum(bool(r["missed_changes"]) for r in results),
        "missed_changes": sum(len(r["missed_changes"]) for r in results),
        "turns_with_dropped_fields": sum(bool(r["dropped_fields"]) for r in results),
        "dropped_fields": sum(len(r["dropped_fields"]) for r in results),
        "results": results,
    }


def fixture_client(expected):
    call = SimpleNamespace(type="function_call", name=TOOL_NAME,
                           arguments=json.dumps(expected, ensure_ascii=False))
    return SimpleNamespace(responses=SimpleNamespace(
        create=lambda **_: SimpleNamespace(status="completed", output=[call])
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="정답을 모의 응답으로 사용해 실행 경로 확인")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    parser.add_argument("--output", type=Path, help="평가 결과 JSON 저장 경로")
    args = parser.parse_args()
    cases = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    client = None
    if not args.dry_run:
        if not os.getenv("OPENAI_API_KEY"):
            parser.error("실제 모델 평가는 OPENAI_API_KEY 설정이 필요합니다. --dry-run으로 모의 검증을 실행할 수 있습니다.")
        try:
            from openai import OpenAI, OpenAIError
        except ImportError:
            parser.error("python -m pip install openai 명령으로 SDK를 설치해 주세요.")
        client = OpenAI(timeout=30, max_retries=1)
        try:
            report = evaluate(cases, client, args.model)
        except OpenAIError as exc:
            parser.exit(1, f"모델 호출 실패 ({type(exc).__name__}). 키, 모델 접근 권한, 연결을 확인해 주세요.\n")
    else:
        report = evaluate(cases, None, args.model, dry_run=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("모의 실행 검증" if args.dry_run else f"실제 모델 평가 ({args.model})")
    print(f"정답 일치 {report['exact_matches']}/{report['turns']}턴 · 호출 오류 {report['invalid_calls']}턴")
    print(f"말하지 않은 조건 추가 {report['turns_with_invented_fields']}턴 · 변경 미반영 {report['turns_with_missed_changes']}턴 · 기존 조건 유실 {report['turns_with_dropped_fields']}턴")
    for row in report["results"]:
        if not row["exact_match"]:
            print(f"- {row['case']} 턴 {row['turn']}: {row['differences'] or '유효한 호출 없음'}")


if __name__ == "__main__":
    main()
