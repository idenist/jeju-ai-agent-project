"""Run from the product root: python -m agents.juneok.cli --check/--demo."""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from .config import PROJECT_ROOT, Settings
from .state import ScenarioStore, demo_scenario
from .tools import execute_tool


def offline_demo() -> list[dict]:
    store = ScenarioStore()
    store.update(demo_scenario(), 0)
    results = [{"tool": "compare_shipment_options", "result": execute_tool("compare_shipment_options", {"expected_version": 1}, store)},
               {"tool": "get_historical_shipment_context", "result": execute_tool("get_historical_shipment_context",
                    {"shipment_variety": "노지온주", "target_month": 11, "start_year": 2018, "end_year": 2024}, store)}]
    options = deepcopy(store.scenario["options"])
    options[0]["loss_percent"] = 4
    store.update({"options": options}, 1)
    results.append({"tool": "compare_shipment_options", "result": execute_tool("compare_shipment_options", {"expected_version": 2}, store)})
    results.append({"tool": "get_historical_shipment_context", "result": execute_tool("get_historical_shipment_context",
                    {"shipment_variety": "노지온주", "target_month": 11, "start_year": 2025, "end_year": 2025}, store)})
    return results


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="감귤 출하 대안 비교 Agent와 오프라인 도구 점검")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--demo", action="store_true")
    group.add_argument("--question")
    group.add_argument("--chat", action="store_true")
    parser.add_argument("--save-report", action="store_true")
    parser.add_argument("--experimental-forecast", action="store_true",
                        help="명시적으로 연구용 ML 가격 추정 도구를 대화에만 등록 (실제 출하 권고 금지)")
    args = parser.parse_args()
    if args.experimental_forecast and not (args.chat or args.question is not None):
        parser.error("연구용 ML 연결은 --chat 또는 --question과 함께 사용하세요.")
    if args.check:
        print(json.dumps(Settings.load().public_status(), ensure_ascii=False, indent=2))
        return 0
    if args.demo:
        from .reporting import render_report
        results = offline_demo()
        if [x["result"]["status"] for x in results] != ["OK", "OK", "OK", "NO_DATA"]:
            print("오프라인 시연의 예상 결과와 다릅니다. 데이터·계산 결과를 확인하세요.")
            return 1
        text = "# 오프라인 도구 시연\n\n사용자가 선택한 가상 예시입니다. 실제 모델 대화 검증과 구분합니다.\n\n" + render_report(results)
    else:
        from .agent import AgentSession
        session = AgentSession(experimental_forecast=args.experimental_forecast)
        if args.chat:
            while True:
                try:
                    question = input("\n질문 (종료: exit)> ").strip()
                except (EOFError, KeyboardInterrupt):
                    return 0
                if question.lower() in {"exit", "종료"}:
                    return 0
                result = session.run(question)
                print(result["text"])
        result = session.run(args.question)
        text = result["text"]
        print(text)
        return 0 if result["status"] == "OK" else 1
    print(text)
    if args.save_report:
        path = PROJECT_ROOT / "artifacts" / "offline-demo.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"\n저장: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
