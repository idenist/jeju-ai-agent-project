"""Prepare frozen extension demo files without live model or data API requests."""
from datetime import datetime, timezone, timedelta
import json
from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.workspace import SharedWorkspace
from agents.juneok.reporting import render_report
from agents.juneok.pdf_report import render_pdf_report


def main():
    folder = PROJECT_ROOT / "artifacts/review-demo" / datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%dT%H%M%S")
    folder.mkdir(parents=True)
    ws = SharedWorkspace(AgentSession(Settings(api_key="", model="offline")))
    ws.load_demo()
    def save(name):
        entries = ws.current_entries()
        (folder / (name + ".pdf")).write_bytes(render_pdf_report(entries))
        (folder / (name + ".md")).write_text(render_report(entries), encoding="utf-8")
        (folder / (name + ".json")).write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    query = {"shipment_variety": "노지온주", "target_month": 11, "start_year": 2018, "end_year": 2024}
    ws.review(shipment_query=query, loss_values=[1, 2, 4], price_values=[3000, 3300])
    save("01_통합검토_가상조건과실제과거자료")
    case = ws.export_case("시연용 출하 검토 · 가상 조건")
    (folder / "비교복원예시.json").write_bytes(case)
    ws.review(shipment_query={**query, "start_year": 2025, "end_year": 2025})
    save("02_미관측_부분검토")
    ws.explain_conditions("seven_days", [1, 2, 4], [3000, 3300])
    save("03_판단조건_지정한6조합")
    result = ws.restore_case(case, ws.store.version)
    assert result["status"] == "OK"
    save("04_복원후_현재조건재계산")
    metadata = {"scope": "OFFLINE_FICTIONAL_CONDITIONS_REAL_CSV_NOT_HUMAN_TEST", "live_model_calls": 0,
                "output_dir": str(folder), "cases": 4, "restore_passed": True}
    (PROJECT_ROOT / "artifacts/review-demo/latest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
