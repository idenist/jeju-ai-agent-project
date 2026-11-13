"""Paid integration checks for the three approved extensions; no credentials logged."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.workspace import SharedWorkspace
from agents.juneok.reporting import render_report


def main():
    settings = Settings.load()
    if not settings.api_key:
        print("모델 키가 없어 실제 호출 검증을 실행하지 않았습니다.")
        return 2
    ws = SharedWorkspace(AgentSession(settings))
    ws.load_demo()
    before = ws.store.snapshot()
    turns = []
    def run(question):
        reply = ws.run(question)
        turns.append({"question": question, "reply": reply})
        return reply
    first = run("저장된 가상 조건은 변경하지 말고 출하 검토 브리핑을 만들어주세요. 출하통계는 노지온주 11월 2018~2024년입니다. 감모 가정은 1%,2%,4%, 미래 단가 가정은 3000원/kg,3300원/kg입니다. 시장 참고는 조회하지 않습니다. 자료와 조건을 함께 검토해주세요.")
    visible = first.get("visible_results", [])
    checks = {"brief_tool_called": any(x["tool"] == "build_review_brief" for x in first.get("tool_results", [])),
              "brief_history_seven_years": any(x["tool"] == "get_historical_shipment_context" and len(x["result"].get("series", [])) == 7 for x in visible),
              "brief_six_explicit_combinations": any(x["tool"] == "compare_sensitivity" and len(x["result"].get("rows", [])) == 6 for x in visible),
              "brief_preserves_conditions": before == ws.store.snapshot()}
    second = run("같은 저장 조건으로 이번에는 출하통계 노지온주 2025년 11월만 포함한 검토 브리핑을 만들어주세요. 시장 참고와 가정 범위는 생략하고, 없는 자료는 없다고 표시해주세요.")
    checks["no_data_stays_partial"] = any(x["tool"] == "build_review_brief" and x["result"].get("review_status") == "PARTIAL" for x in second.get("tool_results", [])) and any(x["tool"] == "get_historical_shipment_context" and x["result"]["status"] == "NO_DATA" for x in second.get("visible_results", []))
    third = run("저장 조건은 그대로 두고 감모 가정 1%,2%,4%와 미래단가 가정 3000원/kg,3300원/kg에서 판단이 바뀌는 조건을 explain_decision_conditions로 설명해주세요. 예측이나 최적 추천은 하지 마세요.")
    checks["explanation_six_cases"] = any(x["tool"] == "explain_decision_conditions" and len(x["result"].get("cases", [])) == 6 for x in third.get("visible_results", []))
    raw = ws.export_case("실제 모델 연결 후 복원 검증 · 가상 조건")
    restored = SharedWorkspace(AgentSession(settings))
    result = restored.restore_case(raw, restored.store.version)
    fourth = restored.run("지금 저장된 조건을 바꾸지 말고 필요한 최소 단가만 다시 비교해주세요.")
    turns.append({"question": "복원 후 같은 조건 재비교", "reply": fourth})
    checks["restored_model_uses_same_3109"] = result["status"] == "OK" and "3,109원/kg" in render_report(fourth.get("visible_results", []))
    checks["all_turns_completed"] = all(t["reply"]["status"] == "OK" for t in turns)
    checks["final_original_conditions_preserved"] = before == ws.store.snapshot()
    folder = PROJECT_ROOT / "artifacts/extensions-live" / datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%dT%H%M%S")
    folder.mkdir(parents=True)
    report = {"scope": "LIVE_MODEL_FICTIONAL_CONDITIONS_REAL_CSV_NOT_HUMAN_TEST", "model": settings.model,
              "checks": checks, "passed": all(checks.values()), "turns": turns,
              "model_requests": sum(t["reply"].get("metrics", {}).get("model_requests", 0) for t in turns),
              "source_sha256": {p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in (PROJECT_ROOT / "agents/juneok").glob("*.py")}}
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROJECT_ROOT / "artifacts/extensions-live/latest.json").write_text(json.dumps({"evidence": str(folder / "result.json"), "checks": checks, "passed": report["passed"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": checks, "passed": report["passed"], "model_requests": report["model_requests"], "evidence": str(folder / "result.json")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
