"""Repeat natural Korean tasks with bounded context; explicit paid API evaluation."""
import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.harness import ExecutionPolicy
from agents.juneok.workspace import SharedWorkspace


def minimum(entries):
    return next((x["result"]["options"][0]["minimum_whole_krw_per_kg"] for x in entries
                 if x["tool"] == "compare_shipment_options" and x["result"].get("status") == "OK"), None)


def history(entries):
    return next((x["result"] for x in entries if x["tool"] == "get_historical_shipment_context"), {})


def trial(case, settings):
    ws = SharedWorkspace(AgentSession(settings, policy=replace(ExecutionPolicy(), history_turns=1)))
    ws.load_demo()
    turns = []
    def run(question):
        reply = ws.run(question)
        turns.append({"question": question, "reply": reply})
        return reply.get("visible_results", [])
    checks = {}
    if case == "H01":
        entries = run("현재 저장한 가상 출하 조건은 바꾸지 말고 노지온주 2018년부터 2024년까지 11월의 과거 출하실적과 함께 검토해줘. 경매 참고와 가정 범위는 생략할게.")
        checks = {"seven_years": len(history(entries).get("series", [])) == 7, "minimum_3109": minimum(entries) == "3109"}
    elif case == "H02":
        run("노지온주의 2018~2024년 11월 과거 출하량을 조회해줘.")
        entries = run("방금 선택한 품종과 기간을 그대로 사용해서 과거 출하자료를 다시 확인해줘. 비교 조건은 변경하지 마.")
        context = ws.session.task_context.snapshot()
        checks = {"selection_survives_trim": len(history(entries).get("series", [])) == 7,
                  "explicit_variety_retained": (context.get("shipment_query") or {}).get("shipment_variety") == "노지온주",
                  "older_turn_removed": turns[-1]["reply"].get("metrics", {}).get("trimmed_history_turns", 0) > 0}
    elif case == "H03":
        run("지금 제시가격은 아직 정해지지 않았어. 기존 현재 단가를 철회해서 미확인으로 바꿔줘. 다른 조건은 그대로야.")
        entries = run("그 상태에서 필요한 최소 단가를 확인해줘. 모르는 값은 채우지 마.")
        checks = {"price_stays_retracted": ws.store.scenario["current_price_per_kg"] is None,
                  "asks_missing_price": any("current_price_per_kg" in x["result"].get("missing_fields", []) for x in entries),
                  "no_fabricated_success": minimum(entries) is None}
    elif case == "H04":
        run("보관기간을 7일에서 3일로 바꿔줘. 이번 3일의 기간 전체 감모는 아직 모르니 미확인으로 두고 다른 조건은 유지해줘.")
        checks["new_period_requires_loss"] = ws.store.scenario["options"][0]["loss_percent"] is None
        entries = run("이번 3일 보관기간 전체 감모를 2%라는 사용자 가정으로 확인할게. 다른 조건은 그대로 비교해줘.")
        checks["reconfirmed_3082"] = minimum(entries) == "3082"
    elif case == "H05":
        entries = run("현재 가상 출하 조건과 노지온주 2025년 11월만의 출하자료를 함께 검토해줘. 없는 자료는 없다고 표시하고 시장 참고는 생략해줘.")
        checks = {"no_data_not_zero": history(entries).get("status") == "NO_DATA" and history(entries).get("series") == [],
                  "comparison_preserved": minimum(entries) == "3109",
                  "partial_review": any(x["result"].get("review_status") == "PARTIAL" for x in entries)}
    elif case == "H06":
        options = deepcopy(ws.store.scenario["options"]); options[0]["loss_percent"] = None
        ws.store.update({"options": options}, ws.store.version); ws.calculate()
        entries = run("실제 감모는 아직 몰라. 이번 기간 전체 감모 1%,2%,4%와 미래 단가 3000원/kg,3300원/kg를 가정해서 각 조합이 지금 출하보다 유리한지 비교해줘. 이 가정으로 기존 실제 감모의 미확인을 덮어쓰지 마.")
        rows = next((x["result"].get("cases", x["result"].get("rows", [])) for x in entries
                     if x["result"].get("analysis_type") in {"sensitivity", "decision_conditions"}), [])
        checks = {"six_explicit_cases": len(rows) == 6, "unknown_actual_loss_preserved": ws.store.scenario["options"][0]["loss_percent"] is None}
    checks["all_turns_completed"] = all(x["reply"]["status"] == "OK" for x in turns)
    checks["quantity_preserved"] = ws.store.scenario["quantity_kg"] == 3000
    checks["inputs_bounded"] = all(max(x["reply"].get("metrics", {}).get("request_input_characters", [0])) <= 32000 for x in turns)
    return {"case": case, "checks": checks, "passed": all(checks.values()), "turns": turns}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, choices=[1, 2, 3], default=3)
    parser.add_argument("--case", choices=[f"H0{i}" for i in range(1, 7)])
    args = parser.parse_args()
    settings = Settings.load()
    if not settings.api_key:
        print("키가 없어 실제 모델 검증을 실행하지 않았습니다."); return 2
    folder = PROJECT_ROOT / "artifacts/task-harness-live" / datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%dT%H%M%S")
    folder.mkdir(parents=True)
    report = {"scope": "LIVE_NATURAL_KOREAN_TASKS_ONE_TURN_HISTORY_NOT_HUMAN_TEST", "model": settings.model,
              "source_sha256": {p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (PROJECT_ROOT / "agents/juneok").glob("*.py")}, "trials": []}
    for case in ([args.case] if args.case else [f"H0{i}" for i in range(1, 7)]):
        for iteration in range(args.repeat):
            result = trial(case, settings); result["iteration"] = iteration + 1
            report["trials"].append(result)
            report["completed"] = len(report["trials"])
            report["passed_trials"] = sum(t["passed"] for t in report["trials"])
            report["passed"] = all(t["passed"] for t in report["trials"])
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            latest = {"evidence": str(folder / "result.json"), "completed": report["completed"], "passed_trials": report["passed_trials"], "passed": report["passed"]}
            (PROJECT_ROOT / "artifacts/task-harness-live/latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": case, "iteration": iteration + 1, "passed": result["passed"], "checks": result["checks"]}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
