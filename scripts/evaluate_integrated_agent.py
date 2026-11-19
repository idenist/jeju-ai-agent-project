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
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from time import perf_counter
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = ROOT / "evals" / "integration_e2e_cases.json"
KST = timezone(timedelta(hours=9))
# The legacy November 15 fixtures were designed as future storage decisions.
EVALUATION_DATE = date(2026, 11, 14)
SETUPS = {"empty", "demo", "direct_history", "demo_with_direct_history", "candidate_comparison"}
CANDIDATE_DATES = (date(2026, 11, 23), date(2026, 11, 28), date(2026, 11, 30))
CANDIDATE_EXPECTATIONS = {
    "opens", "pending_date", "pending_kind", "dates", "prices", "conditions", "losses",
    "next_field", "next_day", "deferred_fields", "storage_pause", "report", "report_fields", "keep_conditions",
}
_FORECAST_LIMIT = re.compile(
    r"(?:미래\s*가격|다음\s*주|실제\s*(?:판매\s*)?단가|과거\s*(?:가격|자료|변동))[^.\n]{0,120}"
    r"(?:없습니다|없어요|없다|어렵|하지\s*않|할\s*수\s*없|아닙니다|아니에요|안\s*됩니다)"
)
_WON_CLAIM = re.compile(r"(?:\d[\d,.]*|[일이삼사오육칠팔구십백천만억]+)\s*(?:천|만|억)?\s*원")
PRICE_QUERY = {
    "variety": "조생귤", "grade": "특", "size": "소", "package": "상자",
    "package_kg": "5", "interval_days": 7, "limit": 3,
}
SOURCE_PATHS = (
    "app.py", "ui/application.py", "ui/context.py", "ui/chat.py", "ui/panels.py", "agents/agent.py", "agents/tools.py",
    "ui/workspace.py", "agents/state.py",
    "agents/future_pair_state.py", "agents/future_pair_intent.py",
    "agents/box_price.py", "agents/prompts.py",
    "agents/shipment_candidates.py", "agents/candidate_storage_billing.py", "agents/dialogue.py",
    "agents/evidence_guard.py", "agents/condition_policy.py",
    "agents/service_scope.py",
    "presenters/presentation.py", "presenters/reporting.py",
    "services/future_pair.py", "services/data_contract.py", "services/market_snapshot.py",
    "services/calculation_service.py", "services/price_history_service.py",
    "services/price_history_followup.py", "services/price_history.py",
    "services/comparison_input.py", "services/price_scenario.py",
    "services/shipment_timing.py", "services/shipment_timing_bundle.py", "services/shipment_candidates.py",
    "services/data_refresh.py", "services/task_context.py",
    "ui/shipment_timing_ui.py", "ui/shipment_candidates.py", "ui/pdf_report.py",
    "data/processed/daily_prices.csv", "data/processed/price_comparison_availability.csv",
    "data/processed/price_comparisons.csv", "data/metadata/juneok_manifest.json",
    "data/processed/daily_shipments.csv", "data/reports/shipment_quality_report.json",
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
        if not isinstance(turns, list) or not 1 <= len(turns) <= 12:
            raise ValueError(f"{case['id']}: 평가 단계는 1~12개여야 합니다.")
        for index, turn in enumerate(turns, 1):
            if not isinstance(turn, dict) or ("candidate_form" not in turn and (
                    not isinstance(turn.get("user"), str) or not turn["user"].strip())):
                raise ValueError(f"{case['id']} {index}턴: 질문 또는 폼 입력이 없습니다.")
            if not isinstance(turn.get("expect", []), list) or not isinstance(turn.get("forbid_tools", []), list):
                raise ValueError(f"{case['id']} {index}턴: 도구 기대값이 잘못됐습니다.")
            if not isinstance(turn.get("forbid_success", []), list):
                raise ValueError(f"{case['id']} {index}턴: 성공 금지 목록이 잘못됐습니다.")
            for expected in turn.get("expect", []):
                if not isinstance(expected, dict) or not isinstance(expected.get("tool"), str) or not expected["tool"]:
                    raise ValueError(f"{case['id']} {index}턴: 기대 도구 이름이 없습니다.")
                if not isinstance(expected.get("fields", {}), dict):
                    raise ValueError(f"{case['id']} {index}턴: 기대 결과 필드가 잘못됐습니다.")
                if type(expected.get("model_selected", True)) is not bool:
                    raise ValueError(f"{case['id']} {index}턴: 기대 도구의 모델 호출 여부가 잘못됐습니다.")
                if "count" in expected and (type(expected["count"]) is not int or expected["count"] < 1):
                    raise ValueError(f"{case['id']} {index}턴: 기대 조회 횟수가 잘못됐습니다.")
                references = expected.get("same_fields_as_turn", {})
                if not isinstance(references, dict) or any(
                    not field.strip() or type(turn_number) is not int or not 1 <= turn_number < index
                    for field, turn_number in references.items()
                ):
                    raise ValueError(f"{case['id']} {index}턴: 비교할 이전 턴 참조가 잘못됐습니다.")
            if "future_pair_has_result" in turn and type(turn["future_pair_has_result"]) is not bool:
                raise ValueError(f"{case['id']} {index}턴: 현재 미래 출하 결과 기대값이 잘못됐습니다.")
            if "future_pair_manual_pending" in turn and type(turn["future_pair_manual_pending"]) is not bool:
                raise ValueError(f"{case['id']} {index}턴: 직접 단가 입력 대기 기대값이 잘못됐습니다.")
            for field in ("history_fields", "model_args"):
                if not isinstance(turn.get(field, {}), dict):
                    raise ValueError(f"{case['id']} {index}턴: {field} 형식이 잘못됐습니다.")
            if not isinstance(turn.get("history_absent", []), list):
                raise ValueError(f"{case['id']} {index}턴: 조건 철회 목록이 잘못됐습니다.")
            if "forecast_refusal" in turn and type(turn["forecast_refusal"]) is not bool:
                raise ValueError(f"{case['id']} {index}턴: 미래 단가 거절 조건이 잘못됐습니다.")
            if "candidate_form" in turn:
                form = turn["candidate_form"]
                can_submit = isinstance(form, dict) and (
                    (form.get("save_initial") is True and not set(form) - {"save_initial", "quotes"}
                     and isinstance(form.get("quotes", {}), dict))
                    or form == {"basis": "gross"} or form == {"basis": "commission_net"})
                if case["setup"] != "empty" or "candidate" not in turn or "user" in turn or not can_submit:
                    raise ValueError(f"{case['id']} {index}턴: 후보일 폼 입력 동작이 잘못됐습니다.")
            if "require_model" in turn and type(turn["require_model"]) is not bool:
                raise ValueError(f"{case['id']} {index}턴: 실제 모델 호출 기대값이 잘못됐습니다.")
            if "text" in turn and (not isinstance(turn["text"], list) or any(
                    not isinstance(part, str) or not part for part in turn["text"])):
                raise ValueError(f"{case['id']} {index}턴: 응답에 포함될 문구가 잘못됐습니다.")
            if "candidate" in turn:
                candidate = turn["candidate"]
                if (case["setup"] not in {"empty", "candidate_comparison"} or not isinstance(candidate, dict)
                        or set(candidate) - CANDIDATE_EXPECTATIONS
                        or candidate.get("report") not in {"same", "changed", "none"}
                        or candidate.get("pending_kind") not in {None, "weight", "basis"}
                        or candidate.get("storage_pause") not in {None, "waiting", "later"}
                        or not isinstance(candidate.get("dates", []), list)
                        or not isinstance(candidate.get("conditions", {}), dict)
                        or not isinstance(candidate.get("losses", {}), dict)
                        or not isinstance(candidate.get("prices", {}), dict)
                        or not isinstance(candidate.get("deferred_fields", []), list)
                        or not isinstance(candidate.get("report_fields", {}), dict)
                        or type(candidate.get("opens", False)) is not bool
                        or type(candidate.get("keep_conditions", True)) is not bool):
                    raise ValueError(f"{case['id']} {index}턴: 후보일 기대 상태가 잘못됐습니다.")
    return cases


def preflight() -> dict:
    """유료 평가 전에 실제 동결 가격쌍과 통합 도구 등록 상태를 확인합니다."""
    from services.data_contract import verify_integrated_price_sources
    from services.price_history_service import get_integrated_price_history
    from agents.tools import TOOLS

    source = verify_integrated_price_sources(ROOT)
    if source.get("status") != "OK" or source.get("full_content_verified") is not True:
        raise RuntimeError("통합 가격 데이터 전체 검증에 실패했습니다: " + str(source.get("reason")))
    benchmarks = {
        (3, 7, 14): (4776, ((7, 20), (14, 15))),
        (3, 5, 7, 10, 14): (7960, ((5, 18), (7, 20), (10, 16), (14, 15))),
    }
    rows, expected_counts = benchmarks.get(tuple(source["available_intervals"]), (None, ()))
    if source["verified_availability_rows"] != rows:
        raise RuntimeError("가격 비교 자료의 검증된 행 수가 평가 기준과 다릅니다.")
    observed = {}
    for interval, expected in expected_counts:
        result = get_integrated_price_history({**PRICE_QUERY, "interval_days": interval})
        if result.get("status") != "OK" or result.get("clean_pairs") != expected:
            raise RuntimeError(f"{interval}일 가격쌍이 평가 기준과 일치하지 않습니다: {result.get('status')}")
        observed[str(interval)] = result["clean_pairs"]
    names = {tool["name"] for tool in TOOLS}
    required = {"get_price_history", "compare_shipment_options", "compare_future_pair"}
    if not required.issubset(names) or "get_price_scenarios" in names:
        raise RuntimeError("통합 Agent의 활성 도구 계약이 변경됐습니다. 평가 기준을 확인하세요.")
    return {"status": "OK", "verified_availability_rows": rows, "snapshot_id": source["snapshot_id"],
            "clean_pairs_by_interval": observed, "activated_scenario_tool": False}


def source_hashes(root: Path = ROOT) -> dict[str, str]:
    """실제 모델과 평가 데이터가 어느 코드·자료 버전에서 실행됐는지 고정합니다."""
    result = {}
    for item in SOURCE_PATHS:
        path = Path(root) / item
        if not path.is_file():
            raise FileNotFoundError(f"평가 기준 파일이 없습니다: {item}")
        result[item] = hashlib.sha256(path.read_bytes()).hexdigest()
    from services.market_snapshot import MARKET_POINTER_FILE, SNAPSHOT_FILES, resolve_market_root

    root = Path(root)
    selected = resolve_market_root(root)
    if selected != root:
        for path in [root / MARKET_POINTER_FILE, selected / "manifest.json",
                     *(selected / item for item in sorted(SNAPSHOT_FILES) if (selected / item).is_file())]:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
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
               history_query: dict | None, calls: list[dict], *, previous_turns=()) -> dict:
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
        if "count" in expected:
            visible_count = sum(row.get("tool") == expected["tool"] for row in reply.get("visible_results", []))
            if len(matching) != expected["count"] or visible_count != expected["count"]:
                failures.append("wrong_tool_count:" + expected["tool"])
        result = matching[-1]
        selected_by_model = any(call["tool"] == expected["tool"] for call in calls)
        if selected_by_model != expected.get("model_selected", True):
            reason = "unexpected_model_tool_selection:" if selected_by_model else "expected_tool_not_selected_by_model:"
            failures.append(reason + expected["tool"])
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
        for field, turn_number in expected.get("same_fields_as_turn", {}).items():
            previous_results = [row["result"] for row in previous_turns[turn_number - 1]["tool_results"]
                                if row.get("tool") == expected["tool"]]
            try:
                if not previous_results or read_path(result, field) != read_path(previous_results[-1], field):
                    failures.append(f"changed_field:{expected['tool']}:{field}:turn_{turn_number}")
            except KeyError:
                failures.append(f"missing_comparison_field:{expected['tool']}:{field}:turn_{turn_number}")
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
    if spec.get("forecast_refusal"):
        answer = reply.get("text", "")
        if not _FORECAST_LIMIT.search(answer):
            failures.append("forecast_limit_not_explained")
        # This case has no entered quote or requested numerical scenario.
        if _WON_CLAIM.search(answer):
            failures.append("unsupported_forecast_price_claim")
    for name, desired in spec.get("history_fields", {}).items():
        if not isinstance(history_query, dict) or name not in history_query or not equal_values(history_query[name], desired):
            failures.append("wrong_history_field:" + name)
    for name in spec.get("history_absent", []):
        if isinstance(history_query, dict) and history_query.get(name) is not None:
            failures.append("inherited_unconfirmed_history_field:" + name)
    reused_calls = set(reply.get("metrics", {}).get("reused_price_history_calls", []))
    for tool, expected_args in spec.get("model_args", {}).items():
        matching = [item["args"] for item in calls if item["tool"] == tool and isinstance(item["args"], dict)
                    and not (tool == "get_price_history" and item.get("call_id") in reused_calls)]
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
    from evals.quality import assess_turn
    return assess_turn(reply)


def create_workspace(settings, setup: str):
    """각 문항마다 독립된 작업공간을 구성하여 이전 문항 오염을 막습니다."""
    from agents.agent import AgentSession
    from ui.workspace import SharedWorkspace
    workspace = SharedWorkspace(AgentSession(settings))
    if setup in {"demo", "demo_with_direct_history"}:
        result = workspace.load_demo()
        if result.get("status") != "OK":
            raise RuntimeError("검수용 가상 손익 조건을 준비하지 못했습니다.")
    if setup in {"direct_history", "demo_with_direct_history"}:
        result = workspace.price_history(PRICE_QUERY)
        if result.get("status") != "OK":
            raise RuntimeError("검수용 직접 과거 가격 조회가 실패했습니다.")
    if setup == "candidate_comparison":
        # Begin with the same known conditions a grower would submit in the candidate form.
        workspace.run("감귤 3톤을 2026년 11월 20일부터 보관해서 11/23, 11/28, 11/30 출하 후보일을 비교해줘.")
        draft = workspace.session.current_shipment_candidates()
        if draft is None or tuple(draft.eligible) != CANDIDATE_DATES:
            raise RuntimeError("검수용 후보일을 열지 못했습니다.")
        draft.common.update(start=date(2026, 11, 20), quantity=3000, basis="gross", fee=5,
                            daily_storage=10000, other_cost=5000)
        draft.by_date.update(zip(CANDIDATE_DATES, ((2500, 1), (None, 3), (2800, 5))))
        draft.result = draft.calculate()
        if draft.result["status"] != "OK":
            raise RuntimeError("검수용 후보일 계산을 준비하지 못했습니다.")
    return workspace


def _candidate_snapshot(workspace):
    from presenters.reporting import render_report
    from ui.chat import _requested_chat_report

    draft = workspace.session.current_shipment_candidates()
    if draft is None:
        return None
    pending = draft.pending_quote
    step = draft.next_condition()
    report = _requested_chat_report(workspace, (), target="comparison")
    document = None
    if report and report["kind"] == "shipment_candidates":
        markdown = render_report([{"tool": "compare_shipment_candidates", "result": draft.result}])
        document = re.search(r"문서 ID: (CIT-[A-F0-9]{12})", markdown).group(1)
    return {"dates": [day.isoformat() for day in draft.eligible],
            "conditions": {key: value.isoformat() if isinstance(value, date) else value
                           for key, value in draft.common.items()},
            "prices": {day.isoformat(): draft.by_date.get(day, (None, None))[0] for day in draft.dates},
            "losses": {day.isoformat(): draft.by_date.get(day, (None, None))[1] for day in draft.dates},
            "pending_date": pending.day.isoformat() if pending else None,
            "pending_kind": None if not pending else "weight" if pending.box else "basis",
            "next_field": step.field if step else None,
            "next_day": step.day.isoformat() if step and step.day else None,
            "deferred_fields": sorted({condition.field for condition in draft.deferred_conditions}),
            "storage_pause": getattr(draft, "storage_pause", None),
            "revision": draft.revision, "report_id": document, "result": deepcopy(draft.result)}


def _candidate_failures(spec, before, after, pair_unchanged):
    opens = spec.get("opens", False)
    if after is None or (before is None and not opens):
        return ["missing_candidate_draft"]
    failures = [] if pair_unchanged else ["unexpected_future_pair_mutation"]
    if opens and before is not None:
        failures.append("candidate_journey_not_fresh")
    for field in ("pending_date", "pending_kind", "dates", "next_field", "next_day", "deferred_fields", "storage_pause"):
        if field in spec and spec[field] != after[field]:
            failures.append("wrong_candidate_field:" + field)
    for group in ("conditions", "losses"):
        for field, desired in spec.get(group, {}).items():
            if not equal_values(after[group].get(field), desired):
                failures.append(f"wrong_candidate_{group}_field:{field}")
    if "prices" in spec:
        expected = spec["prices"]
        if expected.keys() != after["prices"].keys() or any(
            not equal_values(after["prices"][day], price) for day, price in expected.items() if day in after["prices"]
        ):
            failures.append("wrong_candidate_prices")
    if before is not None and spec.get("keep_conditions", True) and any(
        before[key] != after[key] for key in ("conditions", "losses", "dates")
    ):
        failures.append("unexpected_candidate_condition_mutation")
    report = spec["report"]
    if report == "none" and (after["report_id"] is not None or after["result"] is not None):
        failures.append("stale_candidate_report")
    elif report == "same" and (not before or not before["report_id"] or after["report_id"] != before["report_id"]
                               or after["revision"] != before["revision"]):
        failures.append("unexpected_candidate_report_change")
    elif report == "changed" and (not before or not after["report_id"] or after["report_id"] == before["report_id"]
                                   or after["revision"] <= before["revision"]):
        failures.append("candidate_report_not_refreshed")
    for field, desired in spec.get("report_fields", {}).items():
        try:
            actual = read_path(after["result"], field)
        except KeyError:
            failures.append("missing_candidate_report_field:" + field)
            continue
        if not equal_values(actual, desired):
            failures.append("wrong_candidate_report_field:" + field)
    return failures


def _submit_candidate_form(workspace, form):
    """Use the same input methods as the candidate panel without browser widgets."""
    draft = workspace.session.current_shipment_candidates()
    if draft is None:
        return False
    if "save_initial" in form:
        displayed = form.get("quotes")
        quotes = {day: (displayed.get(day.isoformat()) if displayed is not None
                        else draft.by_date.get(day, (None, None))[0]) for day in draft.eligible}
        result = draft.save_initial_inputs(draft.common.get("quantity"), quotes)
        return result["status"] in {"OK", "NEEDS_INPUT"}
    common = {**draft.common, "basis": form["basis"]}
    return draft.save_form_inputs(common, draft.by_date.copy())


def run_case(case: dict, settings, *, as_of: date = EVALUATION_DATE) -> dict:
    with patch("services.data_refresh.kst_today", return_value=as_of):
        return _run_case(case, settings)


def _run_case(case: dict, settings) -> dict:
    """사용자의 자연어 턴만 유료 모델에 전달하고 Python 결과를 채점합니다."""
    workspace = create_workspace(settings, case["setup"])
    outcome = []
    aborted_status = None
    for index, spec in enumerate(case["turns"], 1):
        before = workspace.store.snapshot()
        candidate_spec = spec.get("candidate")
        if candidate_spec:
            candidate_before = _candidate_snapshot(workspace)
            pair_before = workspace.session.future_pair.snapshot()
        previous_ids = {call["call_id"] for call in model_calls(workspace.session)}
        started = perf_counter()
        form = spec.get("candidate_form")
        if form is not None:
            accepted = _submit_candidate_form(workspace, form)
            reply = {"status": "OK" if accepted else "CANDIDATE_FORM_FAILED"}
        else:
            reply = workspace.run(spec["user"])
        elapsed = round(perf_counter() - started, 3)
        after = workspace.store.snapshot()
        calls = [call for call in model_calls(workspace.session) if call["call_id"] not in previous_ids]
        verdict = score_turn(spec, reply, before, after, workspace.session.price_history_query, calls,
                             previous_turns=outcome)
        if spec.get("require_model") and not reply.get("metrics", {}).get("model_requests", 0):
            verdict["failures"].append("missing_real_model_request")
        for phrase in spec.get("text", []):
            if phrase not in reply.get("text", ""):
                verdict["failures"].append("missing_response_phrase:" + phrase)
        quality = turn_quality(reply) if form is None else {"passed": accepted}
        if not quality["passed"]:
            verdict["failures"].append("outcome_quality_check_failed")
        if "future_pair_has_result" in spec:
            current = workspace.session.future_pair.current_result()
            if (current is not None) != spec["future_pair_has_result"]:
                verdict["failures"].append("unexpected_current_future_pair_result")
        if "future_pair_manual_pending" in spec:
            pending = workspace.session.future_pair.pending or {}
            if bool(pending.get("manual_price_required")) != spec["future_pair_manual_pending"]:
                verdict["failures"].append("unexpected_future_pair_manual_pending")
        if candidate_spec:
            candidate_after = _candidate_snapshot(workspace)
            verdict["failures"].extend(_candidate_failures(candidate_spec, candidate_before, candidate_after,
                pair_before == workspace.session.future_pair.snapshot()))
        verdict["passed"] = not verdict["failures"]
        row = {"turn_index": index, "user": spec.get("user"), **verdict, "quality": quality,
               "run_status": reply.get("status"), "text": reply.get("text"), "response_seconds": elapsed,
               "metrics": reply.get("metrics", {}), "model_tool_arguments": calls,
               "tool_results": reply.get("tool_results", []),
               "history_query": workspace.session.price_history_query,
               "scenario_before": before, "scenario_after": after}
        if candidate_spec:
            row.update(candidate_before=candidate_before, candidate_after=candidate_after)
        if form:
            row["candidate_form"] = form
        outcome.append(row)
        if reply.get("status") in {"AUTH_ERROR", "RATE_LIMITED", "API_TIMEOUT", "CONNECTION_ERROR", "API_ERROR"}:
            aborted_status = reply["status"]
            break
    expected_count = len(case["turns"])
    return {"id": case["id"], "category": case["category"], "title": case["title"],
            "passed": len(outcome) == expected_count and all(x["passed"] for x in outcome),
            "completed_turns": len(outcome), "expected_turns": expected_count,
            "abort_reason": aborted_status, "turns": outcome}


def timing_summary(samples):
    return {"samples": samples, "median": round(median(samples), 3) if samples else None,
            "max": max(samples, default=None)}


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
    first_response = {}
    by_case = {}
    for case in cases:
        case_id = case["id"]
        runs = [item for item in reports if item["id"] == case_id]
        first_turns = [item["turns"][0] for item in runs if item["turns"]]
        samples = [turn["response_seconds"] for turn in first_turns if turn["passed"]]
        if samples:
            first_response[case_id] = timing_summary(samples)
        failed_turns = [turn for item in runs for turn in item["turns"] if not turn["passed"]]
        by_case[case_id] = {
            "planned": repeats, "completed": len(runs), "passed": sum(item["passed"] for item in runs),
            "aborts": dict(Counter(item["abort_reason"] for item in runs if item.get("abort_reason"))),
            "first_response_all": timing_summary([turn["response_seconds"] for turn in first_turns]),
            "failed_turn_response": timing_summary([turn["response_seconds"] for turn in failed_turns]),
            "failed_turn_statuses": dict(Counter(turn.get("run_status") or "UNKNOWN" for turn in failed_turns)),
        }
    # Tool count and instruction size are observable cost proxies, not quality scores.
    footprint_turns = [turn.get("metrics", {}) for item in reports for turn in item.get("turns", [])]
    footprint = {
        "tools_offered_per_request": timing_summary([
            x for m in footprint_turns for x in m.get("tools_offered_per_request", [])]),
        "instruction_chars_per_request": timing_summary([
            x for m in footprint_turns for x in m.get("instruction_chars_per_request", [])]),
        "input_chars_per_request": timing_summary([
            x for m in footprint_turns for x in m.get("request_input_characters", [])]),
        "focused_turns": sum(m.get("focused_toolset") is True for m in footprint_turns),
    }
    return {"model": model, "planned_runs": len(cases) * repeats,
            "completed_runs": completed, "passed_runs": passed,
            "pass_rate": round(passed / completed, 4) if completed else None,
            "complete": completed == len(cases) * repeats,
            "categories": categories, "failure_counts": dict(failures), "usage": metrics,
            "request_footprint": footprint,
            "first_response_seconds": first_response, "case_metrics": by_case,
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
    parser.add_argument("--as-of", type=date.fromisoformat, default=EVALUATION_DATE,
                        help="대화의 평가 기준일 YYYY-MM-DD · 기본 2026-11-14 (KST)")
    parser.add_argument("--snapshot-id", help="평가에 사용할 활성 자료 버전 이름 · 다르면 호출 전 중단")
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
        steps = [step for case in selected for step in case["turns"]]
        form_count = sum("candidate_form" in step for step in steps)
        forms = f" / 폼: {form_count}단계" if form_count else ""
        print(f"평가 문항: {len(selected)}개 / 자연어 턴: {len(steps) - form_count}개{forms} · API 호출 없음")
        return 0
    if args.live and not (args.case or args.all):
        parser.error("실제 유료 평가에는 --case INT-01 또는 --all을 명시하세요.")
    try:
        checked = preflight()
    except (RuntimeError, OSError, ValueError, KeyError, ImportError) as exc:
        print("사전검증 실패: " + str(exc), file=sys.stderr)
        return 2
    if args.snapshot_id and checked.get("snapshot_id") != args.snapshot_id:
        print(f"사전검증 실패: 활성 자료 {checked.get('snapshot_id')}가 지정 버전 {args.snapshot_id}와 다릅니다.",
              file=sys.stderr)
        return 2
    if not args.live:
        print(f"통합 평가셋 {len(cases)}개 유효 · 선택 {len(selected)}개 · 과거 자료 검증 OK")
        print(json.dumps(checked, ensure_ascii=False))
        print("실제 모델은 호출하지 않았습니다. 먼저 --live --case INT-01로 확인하세요.")
        return 0
    from core.config import Settings
    from services.task_context import data_revision
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
    from services.market_snapshot import read_market_pointer
    start_revision = data_revision()
    if start_revision == ("DATA_UNAVAILABLE",) or read_market_pointer(ROOT)["snapshot_id"] != checked["snapshot_id"]:
        print("사전검증 뒤 자료 버전이 바뀌었습니다.", file=sys.stderr)
        return 2
    data_hashes = {path: digest for path, digest in hashes.items() if path.startswith(("data/", "artifacts/"))}

    def data_matches():
        if data_revision() != start_revision:
            return False
        try:
            return all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
                       for path, digest in data_hashes.items())
        except OSError:
            return False

    folder = args.out_dir or (ROOT / "artifacts" / "integration-e2e" /
                              datetime.now(KST).strftime("%Y%m%d_%H%M%S_%f"))
    if (folder / "metadata.json").exists() or (folder / "cases.jsonl").exists():
        print("기존 평가 결과가 있어 덮어쓰지 않습니다. 새 --out-dir을 지정하세요.", file=sys.stderr)
        return 2
    folder.mkdir(parents=True, exist_ok=True)
    metadata = {"evaluation": "통합 Agent 실제 E2E STEP7 V1", "started_at_kst": datetime.now(KST).isoformat(),
                "as_of_kst": args.as_of.isoformat(), "snapshot_id": checked["snapshot_id"],
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
            if not data_matches():
                stop_reason = "DATA_REVISION_CHANGED"
                break
            try:
                outcome = run_case(case, settings, as_of=args.as_of)
            except Exception as exc:
                # 예기치 않은 예외에 인증정보·HTTP 본문이 포함될 수 있어 형식만 보관합니다.
                outcome = {"id": case["id"], "category": case["category"], "title": case["title"],
                           "passed": False, "expected_turns": len(case["turns"]),
                           "completed_turns": 0, "abort_reason": "RUNNER_EXCEPTION", "turns": [],
                           "exception_type": type(exc).__name__}
                stop_reason = "RUNNER_EXCEPTION"
            if not data_matches():
                outcome["passed"] = False
                outcome["abort_reason"] = "DATA_REVISION_CHANGED"
                stop_reason = "DATA_REVISION_CHANGED"
            outcome["iteration"] = iteration
            reports.append(outcome)
            with cases_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(outcome, ensure_ascii=False, default=str) + "\n")
            summary = summarize(selected, reports, args.repeat, settings.model)
            summary.update(as_of_kst=args.as_of.isoformat(), snapshot_id=checked["snapshot_id"])
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
    summary.update(as_of_kst=args.as_of.isoformat(), snapshot_id=checked["snapshot_id"])
    summary["stop_reason"] = stop_reason
    _save_json(folder / "summary.json", summary)
    print(f"평가 결과: {summary['passed_runs']}/{summary['completed_runs']}회 통과 "
          f"(계획 {summary['planned_runs']}회) · 기록: {folder}")
    for case_id, row in summary["case_metrics"].items():
        timing = row["first_response_all"]
        if timing["samples"]:
            print(f"{case_id} {row['passed']}/{row['completed']} 통과 · 첫 응답 중앙값 {timing['median']:.3f}초 · 최대 {timing['max']:.3f}초")
        if row["failed_turn_statuses"]:
            failed = row["failed_turn_response"]
            print(f"  실패/중단 {row['failed_turn_statuses']} · 최대 {failed['max']:.3f}초")
        if row["aborts"]:
            print(f"  문항 중단 {row['aborts']}")
    if stop_reason:
        print("평가가 중단됐습니다: " + stop_reason, file=sys.stderr)
        return 2
    return 0 if summary["complete"] and summary["passed_runs"] == summary["planned_runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
