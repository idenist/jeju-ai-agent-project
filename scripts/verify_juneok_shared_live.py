"""Explicit paid cross-entry validation; never prints or stores credentials."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.workspace import SharedWorkspace, card_values
from agents.juneok.reporting import render_report


def main():
    settings = Settings.load()
    if not settings.api_key:
        raise RuntimeError("제품 환경의 모델 키가 필요합니다.")
    ws = SharedWorkspace(AgentSession(settings))
    turns, checks = [], {}
    def run(question):
        reply = ws.run(question)
        turns.append(reply)
        return reply
    def minimum():
        return next((e["result"]["options"][0]["minimum_whole_krw_per_kg"] for e in ws.current_entries()
                     if e["tool"] == "compare_shipment_options" and e["result"].get("status") == "OK"), None)
    first = run("가상 예시로 비교합니다. 물량 3000kg, 지금 제시가격은 수수료만 뺀 3000원/kg입니다. "
                "단가에 이미 반영된 비용은 수수료만입니다. 보관비 외 선별·포장·운송 등 기타 고정비용은 양쪽 동일해서 비교에서 제외합니다. "
                "7일 보관하고 그 7일 전체 감모율은 2%라는 사용자 가정으로 확인합니다. 전체 물량의 하루 추가 보관비는 20000원입니다. "
                "미래 단가는 없고 저장 가능 여부와 출하일 변경 가능 여부는 미확인입니다. 필요한 최소 단가를 계산해주세요.")
    checks["initial_chat_comparison_3109"] = first["status"] == "OK" and minimum() == "3109"
    if not checks["initial_chat_comparison_3109"]:
        raise RuntimeError("초기 실제 대화 비교가 기준을 충족하지 못했습니다.")
    before = ws.store.snapshot()
    values, ids = card_values(before)
    values.update(loss_0="4", input_context=ws.input_context)
    updated = ws.apply_card(values, before["version"], ids)
    checks["card_3174_same_id_and_quantity"] = updated["status"] == "OK" and minimum() == "3174" and (
        ws.store.scenario["options"][0]["id"] == ids[0] and Decimal(str(ws.store.scenario["quantity_kg"])) == 3000)
    third = run("방금 조건 카드에서 감모를 7일 전체 4%로 바꿨습니다. 저장된 다른 조건은 그대로 사용해 다시 비교해주세요.")
    checks["next_chat_uses_card_3174"] = third["status"] == "OK" and minimum() == "3174"
    values, ids = card_values(ws.store.snapshot())
    values.update(days_0=3, period_0=False, input_context=ws.input_context)
    ws.apply_card(values, ws.store.version, ids)
    checks["period_change_requires_confirmation"] = ws.store.scenario["options"][0]["loss_percent"] is None
    last = run("이번 3일 보관기간 전체 감모를 2%라는 사용자 가정으로 새로 확인합니다. 나머지는 그대로 두고 다시 비교해주세요.")
    checks["chat_reconfirms_new_period_3082"] = last["status"] == "OK" and minimum() == "3082"
    from agents.juneok.pdf_report import render_pdf_report
    entries = ws.current_entries()
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%dT%H%M%S")
    folder = PROJECT_ROOT / "artifacts/shared-workspace-live" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    pdf = render_pdf_report(entries)
    (folder / "공유조건보고서.pdf").write_bytes(pdf)
    (folder / "공유조건보고서.md").write_text(render_report(entries), encoding="utf-8")
    from io import BytesIO
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() for p in PdfReader(BytesIO(pdf)).pages)
    checks["pdf_same_minimum_and_version"] = "3,082원/kg" in text and f"조건 버전 {ws.store.version}" in text
    files = ["app.py", *[p.relative_to(PROJECT_ROOT).as_posix() for p in (PROJECT_ROOT / "agents/juneok").glob("*.py")]]
    report = {"live_model_called": True, "model": settings.model, "checks": checks, "passed": all(checks.values()),
              "turns": turns, "state": ws.store.snapshot(), "source_sha256": {
                  p: hashlib.sha256((PROJECT_ROOT / p).read_bytes()).hexdigest() for p in files}}
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": checks, "evidence": str(folder / "result.json")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
