"""Validate cases by default. --live explicitly enables paid model requests."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.quality import assess_turn


def preflight(cases: list[dict]) -> list[dict]:
    """Check evidence fixtures against real files before spending model tokens."""
    from agents.juneok.evidence import get_historical_shipment_context, get_market_evidence
    fixtures = {
        "J05": {"shipment_variety": "노지온주", "target_month": 11, "start_year": 2018, "end_year": 2024},
        "J06": {"shipment_variety": "노지온주", "target_month": 11, "start_year": 2025, "end_year": 2025},
        "J07": {"shipment_variety": "조생귤", "target_month": 11, "start_year": None, "end_year": None},
        "J14": {"market_code": "110001", "variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": 5, "as_of": None},
        "J15": {"market_code": "110001", "variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": 5, "as_of": "2026-11-09"},
    }
    checks = []
    for case in cases:
        args = case.get("evidence_args", fixtures.get(case["id"]))
        if args is None:
            continue
        fn = get_historical_shipment_context if case["expected_tool"] == "get_historical_shipment_context" else get_market_evidence
        result = fn(args)
        checked = score(case, [{"status": "OK", "tool_results": [{"tool": case["expected_tool"], "result": result}]}])
        checks.append(checked)
    return checks


def evaluated_entries(entries):
    """Inspect actual composite outputs; never invent an unexecuted tool result."""
    expanded = []
    for entry in entries:
        expanded.append(entry)
        result = entry["result"]
        if entry["tool"] == "build_review_brief":
            expanded.extend(evaluated_entries(result.get("components", [])))
        if entry["tool"] == "explain_decision_conditions" and result.get("analysis_type") == "decision_conditions":
            expanded.append({"tool": "compare_sensitivity", "result": {**result, "rows": result["cases"]}})
    return expanded


def score(case: dict, turns: list[dict]) -> dict:
    failures = []
    if any(t["status"] != "OK" for t in turns):
        failures.append("model_or_api_error")
    entries = evaluated_entries(turns[-1]["tool_results"]) if turns else []
    matched = [x["result"] for x in entries if x["tool"] == case.get("expected_tool")]
    if case.get("expected_tool"):
        if not matched:
            failures.append("expected_tool_not_called")
        else:
            result = matched[-1]
            if result["status"] != case["expected_status"]:
                failures.append("wrong_tool_status")
            if "minimum_price" in case and (not result.get("options") or result["options"][0]["minimum_whole_krw_per_kg"] != case["minimum_price"]):
                failures.append("wrong_minimum_price")
            def same_number(value, expected):
                try:
                    return Decimal(str(value)) == Decimal(str(expected))
                except (ValueError, TypeError, ArithmeticError):
                    return False
            if "difference_krw" in case and (not result.get("options") or not same_number(result["options"][0].get("difference_krw"), case["difference_krw"])):
                failures.append("wrong_hypothetical_difference")
            if "quantity_kg" in case and not same_number(result.get("input", {}).get("quantity_kg"), case["quantity_kg"]):
                failures.append("changed_quantity")
            if "series_count" in case and len(result.get("series", [])) != case["series_count"]:
                failures.append("wrong_series_count")
            if "option_count" in case and len(result.get("options", [])) != case["option_count"]:
                failures.append("wrong_option_count")
            if "feasibility" in case and result.get("feasibility", {}).get("status") != case["feasibility"]:
                failures.append("wrong_feasibility")
            if case.get("partial_month") and not any("PARTIAL_MONTH" in row.get("warning", []) for row in result.get("series", [])):
                failures.append("partial_month_not_reported")
            if "sensitivity_rows" in case and len(result.get("rows", [])) != case["sensitivity_rows"]:
                failures.append("wrong_sensitivity_row_count")
            for expected_key, field in [("expected_losses", "loss_percent"), ("expected_prices", "future_price_per_kg")]:
                if expected_key in case:
                    actual = {str(Decimal(str(r[field])).normalize()) if r[field] is not None else None for r in result.get("rows", [])}
                    expected = {str(Decimal(str(v)).normalize()) if v is not None else None for v in case[expected_key]}
                    if actual != expected:
                        failures.append("wrong_hypothesis_values:" + field)
            if "preserved_loss" in case:
                actual = turns[-1]["state"]["scenario"]["options"][0].get("loss_percent")
                if actual is not None and case["preserved_loss"] is not None:
                    correct = same_number(actual, case["preserved_loss"])
                else:
                    correct = actual is case["preserved_loss"]
                if not correct:
                    failures.append("changed_base_loss")
    if case.get("forbid_successful_comparison") and any(x["tool"] == "compare_shipment_options" and x["result"]["status"] == "OK" for x in entries):
        failures.append("premature_calculation")
    for name in case.get("required_tools", []):
        if not any(x["tool"] == name and x["result"].get("status") == "OK" for t in turns for x in evaluated_entries(t["tool_results"])):
            failures.append("missing_required_tool:" + name)
    qualities = [assess_turn(t) for t in turns if "visible_results" in t]
    if any(not q["passed"] for q in qualities):
        failures.append("outcome_quality_check_failed")
    return {"id": case["id"], "passed": not failures, "failures": failures, "turn_quality": qualities}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", help="Run one case ID first")
    parser.add_argument("--repeat", type=int, choices=range(1, 4), default=1)
    args = parser.parse_args()
    case_file = PROJECT_ROOT / "evals/juneok_cases.json"
    cases = json.loads(case_file.read_text(encoding="utf-8"))
    if len({c["id"] for c in cases}) != len(cases) or any(not c.get("turns") for c in cases):
        raise ValueError("평가셋 구조가 잘못됐습니다.")
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            parser.error("존재하지 않는 case ID")
    evidence_checks = preflight(cases)
    if any(not row["passed"] for row in evidence_checks):
        print(json.dumps({"invalid_evidence_fixtures": [r for r in evidence_checks if not r["passed"]]}, ensure_ascii=False))
        return 1
    if not args.live:
        print(f"평가셋 {len(cases)}건 및 데이터 전제 {len(evidence_checks)}건 확인. 실제 모델 호출 없음. 실행에는 --live가 필요합니다.")
        return 0
    settings = Settings.load()
    if not settings.api_key:
        print("제품 .env에 OPENAI_API_KEY를 입력하세요. API 호출 없음.")
        return 1
    reports = []
    started = datetime.now(timezone(timedelta(hours=9)))
    artifacts = PROJECT_ROOT / "artifacts"
    archives = artifacts / "evaluation-runs"
    archives.mkdir(parents=True, exist_ok=True)
    path = artifacts / "live-evaluation.json"
    if path.exists():
        prior = archives / (started.strftime("%Y%m%dT%H%M%S%f") + "-previous.json")
        prior.write_bytes(path.read_bytes())
    archive_path = archives / (started.strftime("%Y%m%dT%H%M%S%f") + ".json")
    sources = sorted(str(p.relative_to(PROJECT_ROOT)).replace("\\", "/") for p in (PROJECT_ROOT / "agents/juneok").glob("*.py")) + [
               "scripts/evaluate_juneok.py", "evals/juneok_cases.json", "data/metadata/juneok_manifest.json"]
    metadata = {"model": settings.model, "started_at_kst": started.isoformat(),
                "expected_runs": len(cases) * args.repeat,
                "case_definitions": cases, "evidence_preflight": evidence_checks,
                "source_sha256": {p: hashlib.sha256((PROJECT_ROOT / p).read_bytes()).hexdigest() for p in sources}}
    from agents.juneok.data_refresh import resolve_market_root, market_status
    selected = resolve_market_root()
    metadata["market_snapshot"] = market_status()
    metadata["market_csv_sha256"] = hashlib.sha256((selected / "data/processed/daily_prices.csv").read_bytes()).hexdigest()
    def save():
        payload = {**metadata, "completed_runs": len(reports), "passed_runs": sum(r["passed"] for r in reports), "runs": reports}
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        path.write_text(content, encoding="utf-8")
        archive_path.write_text(content, encoding="utf-8")
    for case in cases:
        for iteration in range(args.repeat):
            session = AgentSession(settings)
            turns = [session.run(question) for question in case["turns"]]
            report = {**score(case, turns), "iteration": iteration + 1, "turns": turns}
            reports.append(report)
            save()
            elapsed = sum(t.get("metrics", {}).get("elapsed_seconds", 0) for t in turns)
            print(f"{case['id']} #{iteration + 1}: {'PASS' if report['passed'] else 'FAIL'} ({elapsed:.1f}s)", flush=True)
    return 0 if all(r["passed"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
