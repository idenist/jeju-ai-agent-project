"""Optional paid-model acceptance eval for semantic routing.

Never runs during unittest discovery. Requires the project's configured API key.
The dataset changes surface wording; it does not mock the meaning extractor.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.juneok.agent import AgentSession
from agents.juneok.config import Settings
from agents.juneok.dialogue_engine import _interpret


def main():
    today = datetime.now(timezone(timedelta(hours=9))).date()
    when = (today + timedelta(days=9)).isoformat()
    samples = [
        (f"{when} 이후부터만 감귤 출하가 됩니다. 언제 팔까요?", "sale_window", "earliest_sale_date", when),
        (f"오늘은 못 팔고, {when}부터 출하 가능해요.", "sale_window", "earliest_sale_date", when),
        (f"가장 빠른 출하 가능일이 {when}예요. 판매 시점을 고민 중입니다.", "sale_window", "earliest_sale_date", when),
        ("감귤을 일주일 더 보관하면 낫겠어요?", "current_comparison", "delay_days", 7),
        ("감귤 3톤을 지금 출하할지 고민 중이에요.", "current_comparison", "quantity_kg", 3000),
    ]
    settings = Settings.load()
    if not settings.api_key:
        print("API key required for live semantic eval; tests cannot substitute for real model calls.")
        return 2
    client = None
    outcomes = []
    for idx, (question, expected_task, expected_key, expected_value) in enumerate(samples, 1):
        session = AgentSession(settings=settings, client=client)
        try:
            parsed, seconds, usage = _interpret(session, question, False, None)
            client = session.client
            ok = parsed["task"] == expected_task and parsed.get(expected_key) == expected_value
            outcomes.append(ok)
            print(f"[{idx}/{len(samples)}] {'PASS' if ok else 'FAIL'} {question}")
            if not ok:
                print("  actual:", json.dumps({"task": parsed["task"], expected_key: parsed.get(expected_key)}, ensure_ascii=False))
        except Exception as exc:
            outcomes.append(False)
            print(f"[{idx}/{len(samples)}] ERROR {type(exc).__name__}: {exc}")
    print(f"Result: {sum(outcomes)}/{len(outcomes)} correct live semantic extractions")
    return 0 if all(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
