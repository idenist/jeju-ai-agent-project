"""Explicit paid Korean tasks for discovery and offer boundaries."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.state import ScenarioStore, demo_scenario
from agents.juneok.quality import assess_turn

CASES = {
    "P01": ("내 조건은 아직 정하지 않았어. 지금 확보한 경매자료에는 어떤 실제 거래규격 조합이 있는지 먼저 찾아서 보여줘. 내 가격이나 품종으로 자동 저장하지 마.", "explore_data", None),
    "P02": ("확보된 경매자료에서 조생귤 5kg 상자의 특 등급, 소 크기인 실제 규격 후보만 찾아줘. 관측기간과 자료 상태도 확인하고 출하통계 품종으로 바꾸지 마.", "explore_data", None),
    "P03": ("현재 저장된 나머지 조건과 7일 보관비는 고정할게. 미래 판매단가는3300원/kg이라는 내 가정으로, 전체 기간 감모가 최대 몇%까지여야 지금 출하보다 비교금액이 낮지 않은지 역산해줘. 실제 감모는 모르는 상태로 유지해줘.", "solve_offer_limits", "loss_percent"),
    "P04": ("현재 7일 전체 감모2% 등 나머지 조건은 고정할게. 미래 단가3300원/kg라는 내 가정에서 전체 물량의 하루 추가 보관비를 최대 얼마까지 감당할 수 있는지 역산해줘. 실제 보관비 미확인은 그대로 유지해.", "solve_offer_limits", "daily_storage_cost"),
    "P05": ("현재 물량과 비용 조건은 바꾸지 말고 7일 후 판매단가가3000원/kg라는 가정일 때 감모율의 허용 상한을 역산해줘. 가능한 비음수 해가 없으면 없다고 해줘.", "solve_offer_limits", None),
    "P06": ("지금 조건에서 허용 감모율을 역산해줘. 미래 제안가격은 아직 모르고 임의로 정하지 않을게. 어떤 입력이 필요한지 확인해줘.", "solve_offer_limits", None),
    "P07": ("앱에서 선택해둔 지난 비교와 지금 조건을 복원하지 말고 나란히 검토해줘. 달라진 조건과 필요한 최소 단가의 차이를 확인하고 지금 조건은 바꾸지 마.", "review_case_changes", None),
    "P08": ("지난 저장 비교와 지금을 변경 검토해줘. 아직 기준 파일은 올리거나 선택하지 않았어. 없는 파일을 만들지 말고 필요한 것을 물어봐.", "review_case_changes", None),
}


def economic_inputs(snapshot):
    value = deepcopy(snapshot["scenario"])
    # Consumed per-patch assertion/version is not an economic input.
    for option in value.get("options", []): option.pop("loss_confirmed_for_period", None)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--case", choices=list(CASES))
    args = parser.parse_args()
    settings = Settings.load()
    folder = PROJECT_ROOT / "artifacts/product-expansion-live" / datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%dT%H%M%S")
    folder.mkdir(parents=True)
    paths = [*sorted((PROJECT_ROOT / "agents/juneok").glob("*.py")), PROJECT_ROOT / "scripts/verify_juneok_product_expansion_live.py"]
    report = {"scope": "FICTIONAL_KOREAN_TASKS_NOT_HUMAN_VALIDATION", "model": settings.model,
              "source_sha256": {p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}, "trials": []}
    for case, (question, tool, unknown) in CASES.items():
        if args.case and args.case != case: continue
        for i in range(args.repeat):
            session = AgentSession(settings)
            scenario = demo_scenario()
            if unknown: scenario["options"][0][unknown] = None
            if case != "P01": session.store = ScenarioStore(scenario)
            if case == "P07":
                from agents.juneok.workspace import SharedWorkspace
                from agents.juneok.casefiles import parse_case
                baseline = SharedWorkspace(AgentSession()); baseline.load_demo()
                session.comparison_reference = parse_case(baseline.export_case("지난 가상 비교"))
                session.comparison_input_context = baseline.input_context
                session.store.scenario["options"][0]["loss_percent"] = 4
            before = deepcopy(session.store.snapshot())
            turn = session.run(question)
            outputs = [x["result"] for x in turn["tool_results"] if x["tool"] == tool]
            result = outputs[-1] if outputs else {}
            checks = {"run_completed": turn["status"] == "OK", "expected_tool_used": bool(outputs), "economic_inputs_unchanged": economic_inputs(session.store.snapshot()) == economic_inputs(before),
                      "canonical_results": assess_turn(turn)["passed"]}
            if case in {"P01", "P02"}:
                checks["real_candidates_bounded"] = result.get("catalog_source") == "market" and result.get("status") == "OK" and 0 < len(result.get("candidates", [])) <= 20
                if case == "P02": checks["specified_joint_filter"] = all(c["conditions"]["variety"] == "조생귤" and c["conditions"]["grade"] == "특" and c["conditions"]["size"] == "소" and c["conditions"]["package"] == "상자" and Decimal(str(c["conditions"]["package_kg"])) == 5 for c in result.get("candidates", []))
            elif case in {"P03", "P04"}: checks["independent_boundary"] = result.get("solve_for") == unknown and result.get("conservative_limit") == ("7.67" if case == "P03" else "100285")
            elif case == "P05": checks["no_feasible_value"] = result.get("boundary_kind") == "NO_FEASIBLE_VALUE"
            elif case == "P06": checks["asks_quote"] = result.get("status") == "NEEDS_INPUT" and "quoted_price_per_kg" in result.get("missing_fields", [])
            elif case == "P07": checks["changes_recomputed"] = bool(result.get("outcomes")) and result["outcomes"][0]["saved_minimum"] == "3109" and result["outcomes"][0]["current_minimum"] == "3174" and result["outcomes"][0]["difference_per_kg"] == "65"
            else: checks["asks_reference"] = result.get("status") == "NEEDS_INPUT" and "comparison_reference" in result.get("missing_fields", [])
            trial = {"case": case, "iteration": i + 1, "checks": checks, "passed": all(checks.values()), "scenario_version_changed": session.store.version != before["version"], "turn": turn}
            report["trials"].append(trial)
            report.update(completed=len(report["trials"]), passed_trials=sum(t["passed"] for t in report["trials"]), passed=all(t["passed"] for t in report["trials"]))
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder.parent / "latest.json").write_text(json.dumps({"evidence": str(folder / "result.json"), "completed": report["completed"], "passed_trials": report["passed_trials"], "passed": report["passed"]}, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": case, "iteration": i + 1, "passed": trial["passed"], "checks": checks}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
