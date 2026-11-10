"""통합 제주 감귤 Agent의 실제 모델 다중 턴 평가를 명시적으로 실행합니다.

기본 실행·--validate·--list는 과금하지 않습니다. --live와 --case 또는
--all을 함께 지정한 경우에만 실제 모델을 호출합니다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = ROOT / "evals" / "integration_e2e_cases.json"
KST = timezone(timedelta(hours=9))
SETUPS = {"empty", "demo", "direct_history", "demo_with_direct_history"}
PRICE_QUERY = {
    "variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
    "package_kg": "5", "interval_days": 7, "limit": 3,
}
SOURCE_PATHS = (
    "app.py", "agents/juneok/agent.py", "agents/juneok/tools.py",
    "agents/juneok/workspace.py", "agents/juneok/state.py",
    "agents/common/calculation_service.py", "agents/common/price_history_service.py",
    "agents/common/price_history_followup.py", "agents/jeonghyeon/price_history.py",
    "data/processed/daily_prices.csv", "data/processed/price_comparison_availability.csv",
    "data/reports/price_comparison_report.json", "evals/integration_e2e_cases.json",
    "scripts/evaluate_integrated_agent.py",
)


def load_cases(path: Path = CASES_FILE) -> list[dict]:
    """평가셋을 불러와 기본 구조와 채점 계약을 검사합니다."""
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("평가셋에는 최소 하나의 문항이 필요합니다.")
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not re.fullmatch(r"INT-\d{2}", str(case.get("id", ""))):
            raise ValueError("평가 문항 ID가 잘못됐습니다.")
        if case["id"] in ids:
            raise ValueError("평가 문항 ID가 중복됐습니다.")
        ids.add(case["id"])
        if case.get("setup") not in SETUPS or not isinstance(case.get("category"), str) or not case["category"]:
            raise ValueError(f"{case['id']}: 사전 준비 또는 범주가 잘못됐습니다.")
        if not isinstance(case.get("title"), str) or not case["title"]:
            raise ValueError(f"{case['id']}: 문항 제목이 없습니다.")
        turns = case.get("turns")
        if not isinstance(turns, list) or not 1 <= len(turns) <= 4:
            raise ValueError(f"{case['id']}: 사용자 턴은 1~4개여야 합니다.")
        for index, turn in enumerate(turns, 1):
            if not isinstance(turn, dict) or not isinstance(turn.get("user"), str) or not turn["user"].strip():
                raise ValueError(f"{case['id']} {index}턴: 질문이 없습니다.")
            if not isinstance(turn.get("expect", []), list) or not isinstance(turn.get("forbid_tools", []), list):
                raise ValueError(f"{case['id']} {index}턴: 도구 기대값이 잘못됐습니다.")
            if not isinstance(turn.get("forbid_success", []), list):
                raise ValueError(f"{case['id']} {index}턴: 성공 금지 목록이 잘못됐습니다.")
            for expected in turn.get("expect", []):
                if not isinstance(expected, dict) or not isinstance(expected.get("tool"), str) or not expected["tool"]:
                    raise ValueError(f"{case['id']} {index}턴: 기대 도구 이름이 없습니다.")
                if not isinstance(expected.get("fields", {}), dict):
                    raise ValueError(f"{case['id']} {index}턴: 기대 결과 필드가 잘못됐습니다.")
            for field in ("history_fields", "model_args"):
                if not isinstance(turn.get(field, {}), dict):
                    raise ValueError(f"{case['id']} {index}턴: {field} 형식이 잘못됐습니다.")
            if not isinstance(turn.get("history_absent", []), list):
                raise ValueError(f"{case['id']} {index}턴: 조건 철회 목록이 잘못됐습니다.")
    return cases


def preflight() -> dict:
    """유료 평가 전에 실제 동결 가격쌍과 통합 도구 등록 상태를 확인합니다."""
    from agents.common.data_contract import verify_integrated_price_sources
    from agents.common.price_history_service import get_integrated_price_history
    from agents.juneok.tools import TOOLS

    source = verify_integrated_price_sources(ROOT)
    if source.get("status") != "OK" or source.get("full_content_verified") is not True:
        raise RuntimeError("통합 가격 데이터 전체 검증에 실패했습니다: " + str(source.get("reason")))
    if source.get("verified_availability_rows") != 4776:
        raise RuntimeError("가격 비교 자료의 검증된 행 수가 평가 기준과 다릅니다.")
    observed = {}
    for interval, expected in ((7, 20), (14, 15)):
        result = get_integrated_price_history({**PRICE_QUERY, "interval_days": interval})
        if result.get("status") != "OK" or result.get("clean_pairs") != expected:
            raise RuntimeError(f"{interval}일 가격쌍이 평가 기준과 일치하지 않습니다: {result.get('status')}")
        observed[str(interval)] = result["clean_pairs"]
    names = {tool["name"] for tool in TOOLS}
    if "get_price_history" not in names or "compare_shipment_options" not in names or "get_price_scenarios" in names:
        raise RuntimeError("통합 Agent의 활성 도구 계약이 변경됐습니다. 평가 기준을 확인하세요.")
    return {"status": "OK", "verified_availability_rows": 4776,
            "clean_pairs_by_interval": observed, "activated_scenario_tool": False}


def source_hashes(root: Path = ROOT) -> dict[str, str]:
    """실제 모델과 평가 데이터가 어느 코드·자료 버전에서 실행됐는지 고정합니다."""
    result = {}
    for item in SOURCE_PATHS:
        path = Path(root) / item
        if not path.is_file():
            raise FileNotFoundError(f"평가 기준 파일이 없습니다: {item}")
        result[item] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def read_path(data, path: str):
    """기대값의 점 표기 경로를 객체와 배열에 대해 읽습니다."""
    value = data
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(path)
    return value


def equal_values(actual, expected) -> bool:
    """수치의 문자열 표현 차이는 허용하지만, 누락·불리언·문자열은 구분합니다."""
    if actual is None or expected is None:
        return actual is expected
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float, Decimal)) and isinstance(expected, (int, float, Decimal)):
        try:
            return Decimal(str(actual)) == Decimal(str(expected))
        except InvalidOperation:
            return False
    if isinstance(actual, (int, float, Decimal)) and isinstance(expected, str):
        try:
            return Decimal(str(actual)) == Decimal(expected)
        except InvalidOperation:
            return False
    if isinstance(expected, (int, float, Decimal)) and isinstance(actual, str):
        try:
            return Decimal(actual) == Decimal(str(expected))
        except InvalidOperation:
            return False
    return type(actual) is type(expected) and actual == expected


def model_calls(session) -> list[dict]:
    """최종 응답 문장이 아닌 실제 모델의 함수 호출 인수를 추출합니다."""
    calls = []
    for message in session.messages:
        if getattr(message, "type", None) != "function_call":
            continue
        try:
            arguments = json.loads(message.arguments)
        except (ValueError, TypeError):
            arguments = None
        calls.append({"call_id": str(getattr(message, "call_id", "")),
                      "tool": str(message.name), "args": arguments})
    return calls


def score_turn(spec: dict, reply: dict, before_state: dict, after_state: dict,
               history_query: dict | None, calls: list[dict]) -> dict:
    """도구 실행 결과, 모델 인수, 시나리오 불변을 서로 독립적으로 채점합니다."""
    failures = []
    if reply.get("status") != "OK":
        failures.append("turn_execution_failed:" + str(reply.get("status")))
    entries = reply.get("tool_results", [])
    for expected in spec.get("expect", []):
        matching = [row["result"] for row in entries if row.get("tool") == expected["tool"]]
        if not matching:
            failures.append("missing_tool:" + expected["tool"])
            continue
        result = matching[-1]
        if not any(call["tool"] == expected["tool"] for call in calls):
            failures.append("expected_tool_not_selected_by_model:" + expected["tool"])
        if expected.get("status") is not None and result.get("status") != expected["status"]:
            failures.append(f"wrong_status:{expected['tool']}:{result.get('status')}")
        for field, desired in expected.get("fields", {}).items():
            try:
                actual = read_path(result, field)
            except KeyError:
                failures.append("missing_field:" + expected["tool"] + ":" + field)
                continue
            if not equal_values(actual, desired):
                failures.append("wrong_field:" + expected["tool"] + ":" + field)
        for name in expected.get("missing_contains", []):
            if name not in result.get("missing_fields", []):
                failures.append("missing_confirmation:" + name)
        for name in expected.get("available_contains", []):
            if name not in result.get("available_intervals", []):
                failures.append("missing_available_interval:" + str(name))
        if expected["tool"] == "get_price_history" and result.get("status") == "OK":
            if result.get("is_future_forecast") is not False:
                failures.append("history_mislabeled_as_forecast")
            if result.get("source_verification", {}).get("status") != "OK":
                failures.append("history_source_not_verified")
    invoked = {item.get("tool") for item in entries}
    invoked.update(item["tool"] for item in calls)
    for forbidden in spec.get("forbid_tools", []):
        if forbidden in invoked:
            failures.append("forbidden_tool:" + forbidden)
    for forbidden in spec.get("forbid_success", []):
        if any(row.get("tool") == forbidden and row.get("result", {}).get("status") == "OK" for row in entries):
            failures.append("forbidden_success:" + forbidden)
    if spec.get("preserve_scenario") and before_state != after_state:
        failures.append("unexpected_scenario_mutation")
    if spec.get("future_price_must_be_empty"):
        if any(opt.get("future_price_per_kg") is not None for opt in after_state.get("scenario", {}).get("options", [])):
            failures.append("future_price_autofilled")
    for name, desired in spec.get("history_fields", {}).items():
        if not isinstance(history_query, dict) or name not in history_query or not equal_values(history_query[name], desired):
            failures.append("wrong_history_field:" + name)
    for name in spec.get("history_absent", []):
        if isinstance(history_query, dict) and history_query.get(name) is not None:
            failures.append("inherited_unconfirmed_history_field:" + name)
    for tool, expected_args in spec.get("model_args", {}).items():
        matching = [item["args"] for item in calls if item["tool"] == tool and isinstance(item["args"], dict)]
        if not matching:
            failures.append("missing_model_call:" + tool)
            continue
        for name, desired in expected_args.items():
            if not equal_values(matching[-1].get(name), desired):
                failures.append("wrong_model_arg:" + tool + ":" + name)
    return {"passed": not failures, "failures": list(dict.fromkeys(failures)),
            "tools_executed": [row.get("tool") for row in entries],
            "model_calls": [row["tool"] for row in calls],
            "expected_tools": [x["tool"] for x in spec.get("expect", [])]}


def turn_quality(reply: dict) -> dict:
    """실제 Python 보고서와 표시 결과의 기존 품질 계약을 확인합니다."""
    from agents.juneok.quality import assess_turn
    return assess_turn(reply)


def create_workspace(settings, setup: str):
    """각 문항마다 독립된 작업공간을 구성하여 이전 문항 오염을 막습니다."""
    from agents.juneok.agent import AgentSession
    from agents.juneok.workspace import SharedWorkspace
    workspace = SharedWorkspace(AgentSession(settings))
    if setup in {"demo", "demo_with_direct_history"}:
        result = workspace.load_demo()
        if result.get("status") != "OK":
            raise RuntimeError("검수용 가상 손익 조건을 준비하지 못했습니다.")
    if setup in {"direct_history", "demo_with_direct_history"}:
        result = workspace.price_history(PRICE_QUERY)
        if result.get("status") != "OK":
            raise RuntimeError("검수용 직접 과거 가격 조회가 실패했습니다.")
    return workspace


def run_case(case: dict, settings) -> dict:
    """사용자의 자연어 턴만 유료 모델에 전달하고 Python 결과를 채점합니다."""
    workspace = create_workspace(settings, case["setup"])
    outcome = []
    aborted_status = None
    for index, spec in enumerate(case["turns"], 1):
        before = workspace.store.snapshot()
        previous_ids = {call["call_id"] for call in model_calls(workspace.session)}
        reply = workspace.run(spec["user"])
        after = workspace.store.snapshot()
        calls = [call for call in model_calls(workspace.session) if call["call_id"] not in previous_ids]
        verdict = score_turn(spec, reply, before, after, workspace.session.price_history_query, calls)
        quality = turn_quality(reply)
        if not quality["passed"]:
            verdict["passed"] = False
            verdict["failures"].append("outcome_quality_check_failed")
        row = {"turn_index": index, "user": spec["user"], **verdict, "quality": quality,
               "run_status": reply.get("status"), "text": reply.get("text"),
               "metrics": reply.get("metrics", {}), "model_tool_arguments": calls,
               "tool_results": reply.get("tool_results", []),
               "history_query": workspace.session.price_history_query,
               "scenario_before": before, "scenario_after": after}
        outcome.append(row)
        if reply.get("status") in {"AUTH_ERROR", "RATE_LIMITED", "API_TIMEOUT", "CONNECTION_ERROR", "API_ERROR"}:
            aborted_status = reply["status"]
            break
    expected_count = len(case["turns"])
    return {"id": case["id"], "category": case["category"], "title": case["title"],
            "passed": len(outcome) == expected_count and all(x["passed"] for x in outcome),
            "completed_turns": len(outcome), "expected_turns": expected_count,
            "abort_reason": aborted_status, "turns": outcome}


def summarize(cases: list[dict], reports: list[dict], repeats: int, model: str) -> dict:
    completed = len(reports)
    passed = sum(item["passed"] for item in reports)
    categories = {}
    for category in sorted({item["category"] for item in reports}):
        group = [item for item in reports if item["category"] == category]
        categories[category] = {"passed": sum(x["passed"] for x in group), "total": len(group)}
    failures = Counter(reason for item in reports for turn in item["turns"] for reason in turn["failures"])
    metrics = {key: sum(turn.get("metrics", {}).get(key, 0) or 0 for item in reports for turn in item["turns"])
               for key in ("model_requests", "input_tokens", "output_tokens", "cached_input_tokens")}
    return {"model": model, "planned_runs": len(cases) * repeats,
            "completed_runs": completed, "passed_runs": passed,
            "pass_rate": round(passed / completed, 4) if completed else None,
            "complete": completed == len(cases) * repeats,
            "categories": categories, "failure_counts": dict(failures), "usage": metrics,
            "estimated_cost_usd": None,
            "note": "API 요금 추정 불가: 모델별 실제 가격 정보 미입력. 실제 청구액은 서비스 사용량에서 확인하세요."}


def _save_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="문항 목록만 확인 · API 미호출")
    mode.add_argument("--validate", action="store_true", help="문항·데이터 사전검증 · API 미호출")
    mode.add_argument("--live", action="store_true", help="실제 모델 호출 · 과금 가능")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--case", action="append", help="실행할 INT-xx 문항 ID (반복 지정 가능)")
    scope.add_argument("--all", action="store_true", help="전체 평가셋 실행")
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--model", help="평가에 사용할 실제 모델 ID · 기본값은 설정값")
    parser.add_argument("--out-dir", type=Path, help="평가 결과 저장 폴더 · 기본 artifacts/integration-e2e/<시각>")
    args = parser.parse_args(argv)
    try:
        cases = load_cases()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    known = {case["id"] for case in cases}
    if args.case and (unknown := sorted(set(args.case) - known)):
        parser.error("알 수 없는 평가 문항: " + ", ".join(unknown))
    selected = [case for case in cases if not args.case or case["id"] in set(args.case)]
    if args.list:
        for case in selected:
            print(f"{case['id']} [{case['category']}] {case['title']} · {len(case['turns'])}턴")
        print(f"평가 문항: {len(selected)}개 / 자연어 턴: {sum(len(x['turns']) for x in selected)}개 · API 호출 없음")
        return 0
    if args.live and not (args.case or args.all):
        parser.error("실제 유료 평가에는 --case INT-01 또는 --all을 명시하세요.")
    try:
        checked = preflight()
    except (RuntimeError, OSError, ValueError, KeyError, ImportError) as exc:
        print("사전검증 실패: " + str(exc), file=sys.stderr)
        return 2
    if not args.live:
        print(f"통합 평가셋 {len(cases)}개 유효 · 선택 {len(selected)}개 · 과거 자료 검증 OK")
        print(json.dumps(checked, ensure_ascii=False))
        print("실제 모델은 호출하지 않았습니다. 먼저 --live --case INT-01로 확인하세요.")
        return 0
    from agents.juneok.config import Settings
    from agents.juneok.task_context import data_revision
    settings = Settings.load()
    if args.model:
        settings = Settings(api_key=settings.api_key, model=args.model.strip())
    if not settings.api_key or not settings.model.strip():
        print("프로젝트 .env의 OPENAI_API_KEY와 모델 설정을 확인하세요. API 호출 없음.", file=sys.stderr)
        return 2
    # 동일 조건 재실행을 위해 코드·평가셋·자료 해시를 모두 기록합니다.
    try:
        hashes = source_hashes()
    except OSError as exc:
        print("기준 파일 확인 실패: " + str(exc), file=sys.stderr)
        return 2
    folder = args.out_dir or (ROOT / "artifacts" / "integration-e2e" /
                              datetime.now(KST).strftime("%Y%m%d_%H%M%S_%f"))
    if (folder / "metadata.json").exists() or (folder / "cases.jsonl").exists():
        print("기존 평가 결과가 있어 덮어쓰지 않습니다. 새 --out-dir을 지정하세요.", file=sys.stderr)
        return 2
    folder.mkdir(parents=True, exist_ok=True)
    start_revision = data_revision()
    metadata = {"evaluation": "통합 Agent 실제 E2E STEP7 V1", "started_at_kst": datetime.now(KST).isoformat(),
                "model": settings.model, "cases": [x["id"] for x in selected], "repeats": args.repeat,
                "expected_turns_per_repeat": sum(len(x["turns"]) for x in selected),
                "preflight": checked, "source_sha256": hashes,
                "revision_at_start": list(start_revision),
                "actual_model_api_called": True,
                "note": "평가 입력은 가상 사례이며 결과에 API 키·요청 헤더를 저장하지 않습니다."}
    _save_json(folder / "metadata.json", metadata)
    reports: list[dict] = []
    cases_path = folder / "cases.jsonl"
    stop_reason = None
    for case in selected:
        for iteration in range(1, args.repeat + 1):
            if data_revision() != start_revision:
                stop_reason = "DATA_REVISION_CHANGED"
                break
            try:
                outcome = run_case(case, settings)
            except Exception as exc:
                # 예기치 않은 예외에 인증정보·HTTP 본문이 포함될 수 있어 형식만 보관합니다.
                outcome = {"id": case["id"], "category": case["category"], "title": case["title"],
                           "passed": False, "expected_turns": len(case["turns"]),
                           "completed_turns": 0, "abort_reason": "RUNNER_EXCEPTION", "turns": [],
                           "exception_type": type(exc).__name__}
                stop_reason = "RUNNER_EXCEPTION"
            if data_revision() != start_revision:
                outcome["passed"] = False
                outcome["abort_reason"] = "DATA_REVISION_CHANGED"
                stop_reason = "DATA_REVISION_CHANGED"
            outcome["iteration"] = iteration
            reports.append(outcome)
            with cases_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(outcome, ensure_ascii=False, default=str) + "\n")
            summary = summarize(selected, reports, args.repeat, settings.model)
            summary["stop_reason"] = stop_reason or outcome.get("abort_reason")
            _save_json(folder / "summary.json", summary)
            print(f"[{len(reports)}/{len(selected)*args.repeat}] {case['id']} #{iteration}: "
                  f"{'PASS' if outcome['passed'] else 'FAIL'} · {outcome['completed_turns']}/{outcome['expected_turns']}턴", flush=True)
            if outcome.get("abort_reason") or stop_reason:
                stop_reason = stop_reason or outcome["abort_reason"]
                break
        if stop_reason:
            break
    summary = summarize(selected, reports, args.repeat, settings.model)
    summary["stop_reason"] = stop_reason
    _save_json(folder / "summary.json", summary)
    print(f"평가 결과: {summary['passed_runs']}/{summary['completed_runs']}회 통과 "
          f"(계획 {summary['planned_runs']}회) · 기록: {folder}")
    if stop_reason:
        print("평가가 중단됐습니다: " + stop_reason, file=sys.stderr)
        return 2
    return 0 if summary["complete"] and summary["passed_runs"] == summary["planned_runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
