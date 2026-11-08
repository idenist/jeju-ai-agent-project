"""Validate one realistic incremental dialogue. --live enables model calls."""
from __future__ import annotations

import argparse
import json
import sys
import hashlib
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings

QUESTIONS = [
    "감귤 3톤을 지금 팔지 7일 더 보관할지 비교해줘. 지금 제시받은 가격은 kg당 3000원이야.",
    "수수료는 이미 뺀 단가고 단가에 포함된 건 수수료뿐이야. 포장·운송·선별 등 다른 고정비는 지금이나 7일 뒤나 같아서 비교에서는 빼도 돼. 보관비는 하루 2만원이야.",
    "7일 동안 전체 감량은 2%로 가정해. 저장할 수 있고 출하일도 바꿀 수 있어.",
    "다른 조건은 그대로 감모율을 4%로 바꾸고, 출하일 변경은 확실하지 않아서 미확인으로 돌려줘.",
]


def check(turns: list[dict]) -> dict:
    checks = {"four_turns": len(turns) == 4, "no_api_errors": all(t["status"] == "OK" for t in turns)}
    if len(turns) != 4:
        return checks
    def latest_comparison(turn):
        found = [x["result"] for x in turn["tool_results"] if x["tool"] == "compare_shipment_options"]
        return found[-1] if found else {}
    for i in (0, 1):
        checks[f"no_premature_calculation_turn_{i+1}"] = not any(
            x["tool"] == "compare_shipment_options" and x["result"]["status"] == "OK" for x in turns[i]["tool_results"])
    third, fourth = latest_comparison(turns[2]), latest_comparison(turns[3])
    checks["third_turn_calculates_3109"] = third.get("status") == "OK" and third.get("options", [{}])[0].get("minimum_whole_krw_per_kg") == "3109"
    checks["third_turn_user_confirmed_feasibility"] = third.get("feasibility", {}).get("status") == "CONFIRMED_BY_USER"
    checks["fourth_turn_calculates_3174"] = fourth.get("status") == "OK" and fourth.get("options", [{}])[0].get("minimum_whole_krw_per_kg") == "3174"
    state = turns[3]["state"]["scenario"]
    try:
        checks["preserved_quantity_and_price"] = Decimal(str(state.get("quantity_kg"))) == 3000 and Decimal(str(state.get("current_price_per_kg"))) == 3000
    except ArithmeticError:
        checks["preserved_quantity_and_price"] = False
    checks["preserved_storage_confirmation"] = state.get("storage_available") is True
    checks["retracted_shipping_confirmation"] = state.get("shipping_flexible") is None
    checks["feasibility_returns_unknown"] = fourth.get("feasibility", {}).get("status") == "UNCONFIRMED"
    return checks


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        print("4턴의 점진적 조건 확인·변경·철회 사례 준비. 실제 모델 호출 없음.")
        return 0
    settings = Settings.load()
    if not settings.api_key:
        print("제품 .env의 키를 확인하세요. 실제 모델 호출 없음.")
        return 1
    session = AgentSession(settings)
    results = []
    path = PROJECT_ROOT / "artifacts/realistic-dialogue.json"
    started = datetime.now(timezone(timedelta(hours=9)))
    archives = PROJECT_ROOT / "artifacts/evaluation-runs"
    archives.mkdir(parents=True, exist_ok=True)
    name = "dialogue-" + started.strftime("%Y%m%dT%H%M%S%f")
    if path.exists():
        (archives / (name + "-previous.json")).write_bytes(path.read_bytes())
    sources = ["agents/juneok/agent.py", "agents/juneok/state.py", "agents/juneok/tools.py",
               "agents/juneok/presentation.py", "scripts/validate_juneok_dialogue.py"]
    metadata = {"model": settings.model, "started_at_kst": started.isoformat(), "questions": QUESTIONS,
                "source_sha256": {p: hashlib.sha256((PROJECT_ROOT / p).read_bytes()).hexdigest() for p in sources}}
    for index, question in enumerate(QUESTIONS):
        result = session.run(question)
        results.append(result)
        payload = json.dumps({**metadata, "completed_turns": len(results), "turns": results, "checks": check(results)}, ensure_ascii=False, indent=2)
        path.write_text(payload, encoding="utf-8")
        (archives / (name + ".json")).write_text(payload, encoding="utf-8")
        print(f"Turn {index+1}: {result['status']} ({result.get('metrics',{}).get('elapsed_seconds',0):.1f}s)", flush=True)
    checks = check(results)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
