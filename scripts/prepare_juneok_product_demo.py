"""Keyless local samples for discovery, inverse boundaries and case changes."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from unittest.mock import patch
from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT
from agents.juneok.workspace import SharedWorkspace
from agents.juneok.pdf_report import render_pdf_report
from agents.juneok.reporting import render_report


def main():
    folder = PROJECT_ROOT / "artifacts/product-demo" / datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%dT%H%M%S")
    folder.mkdir(parents=True)
    checks = {}
    ws = SharedWorkspace(AgentSession()); ws.load_demo()
    original = deepcopy(ws.store.snapshot()); baseline = ws.export_case("감모2%의 저장 비교")
    def export(name):
        entries = ws.current_entries()
        (folder / (name + ".pdf")).write_bytes(render_pdf_report(entries))
        (folder / (name + ".md")).write_text(render_report(entries), encoding="utf-8")
        (folder / (name + ".json")).write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    with patch("requests.sessions.Session.request", side_effect=AssertionError("OFFLINE")), patch("httpx.Client.send", side_effect=AssertionError("OFFLINE")):
        result = ws.explore("market", {"variety": "조생귤", "package_kg": 5}, limit=4)
        checks["observed_joint_candidates"] = result["status"] == "OK" and len(result["candidates"]) == 4
        export("01_실제거래규격탐색")
        result = ws.offer_limits(None, 3300, "loss_percent")
        checks["loss_limit_767"] = result.get("conservative_limit") == "7.67"
        export("02_허용감모율역산")
        result = ws.offer_limits(None, 3300, "daily_storage_cost")
        checks["daily_storage_limit_100285"] = result.get("conservative_limit") == "100285"
        checks["read_only_inputs"] = ws.store.snapshot() == original
        export("03_허용보관비역산")
        options = deepcopy(ws.store.scenario["options"]); options[0]["loss_percent"] = 4
        ws.store.update({"options": options}, ws.store.version)
        before = ws.store.snapshot(); result = ws.compare_case(baseline, ws.store.version)
        checks["changes_3109_to_3174"] = result["outcomes"][0]["difference_per_kg"] == "65"
        checks["comparison_does_not_restore"] = before == ws.store.snapshot()
        export("04_저장비교변경검토")
    (folder / "비교기준예시.json").write_bytes(baseline)
    result = {"scope": "OFFLINE_FICTIONAL_CONDITIONS_REAL_CSV_NOT_HUMAN_TEST", "checks": checks,
              "live_model_calls": 0, "http_blocked": True, "passed": all(checks.values()), "output_dir": str(folder)}
    (folder / "시연검증결과.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder.parent / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
