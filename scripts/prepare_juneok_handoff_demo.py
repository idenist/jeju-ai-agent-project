"""Replay the handoff examples offline and export verified demo material."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from agents.juneok.agent import AgentSession
from agents.juneok.config import PROJECT_ROOT, Settings
from agents.juneok.data_refresh import market_status
from agents.juneok.workspace import SharedWorkspace, card_values
from agents.juneok.reporting import render_report
from agents.juneok.pdf_report import render_pdf_report


class SimulatedUnavailableClient:
    """An explicitly simulated transport failure, never an actual model call."""
    @property
    def responses(self):
        return self

    def create(self, **kwargs):
        raise RuntimeError("SIMULATED_DEMO_TRANSPORT_FAILURE")


def main():
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%dT%H%M%S")
    folder = PROJECT_ROOT / "artifacts/handoff-demo" / stamp
    folder.mkdir(parents=True)
    checks, cases = {}, []
    ws = SharedWorkspace(AgentSession(Settings(api_key="", model="offline-demo")))

    def save(name):
        entries = ws.current_entries()
        (folder / (name + ".json")).write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / (name + ".md")).write_text(render_report(entries), encoding="utf-8")
        pdf = render_pdf_report(entries)
        (folder / (name + ".pdf")).write_bytes(pdf)
        cases.append({"name": name, "scenario_version": ws.store.version,
                      "pdf_sha256": hashlib.sha256(pdf).hexdigest()})

    # All inputs are fictional demonstration assumptions; history is real bundled observation data.
    with patch("requests.sessions.Session.request", side_effect=AssertionError("DEMO_HTTP_DISABLED")), \
         patch("httpx.Client.send", side_effect=AssertionError("DEMO_HTTP_DISABLED")):
        ws.load_demo()
        result = ws.current_entries()[0]["result"]
        checks["D01_minimum_3109"] = result["options"][0]["minimum_whole_krw_per_kg"] == "3109"
        checks["D01_remaining_2940_storage_140000"] = Decimal(result["options"][0]["remaining_kg"]) == 2940 and Decimal(result["options"][0]["storage_cost_krw"]) == 140000
        save("01_기준조건_가상예시")

        before = ws.store.snapshot()
        values, ids = card_values(before)
        values.update(loss_0="4", input_context=ws.input_context)
        ws.apply_card(values, before["version"], ids)
        result = ws.current_entries()[0]["result"]
        checks["D02_minimum_3174"] = result["options"][0]["minimum_whole_krw_per_kg"] == "3174"
        checks["D02_other_values_and_id_preserved"] = all(ws.store.scenario[k] == before["scenario"][k] for k in ["quantity_kg", "current_price_per_kg", "cost_policy"]) and ws.store.scenario["options"][0]["id"] == ids[0]
        save("02_감모수정_가상예시")

        before = ws.store.snapshot()
        result = ws.sensitivity(ids[0], [1, 2, 4], [3000, 3300])
        checks["D03_six_rows_and_original_state_preserved"] = len(result["rows"]) == 6 and before == ws.store.snapshot()
        differences = {Decimal(r["loss_percent"]): Decimal(r["difference_krw"]) for r in result["rows"] if Decimal(r["future_price_per_kg"]) == 3300}
        checks["D03_independent_expected_differences"] = differences == {Decimal(1): Decimal(661000), Decimal(2): Decimal(562000), Decimal(4): Decimal(364000)}
        save("03_감모와가격가정_6조합")

        result = ws.history({"shipment_variety": "노지온주", "target_month": 11, "start_year": 2025, "end_year": 2025})
        checks["D04_missing_period_is_no_data_not_zero"] = result["status"] == "NO_DATA" and result["series"] == []
        save("04_미관측자료_실제CSV")

        before = ws.store.snapshot()
        values, ids = card_values(before)
        values["current_fee"] = "5"
        rejected = ws.apply_card(values, before["version"], ids)
        checks["D05_fee_conflict_rolls_back"] = rejected["status"] == "INVALID_INPUT" and before == ws.store.snapshot()

        from agents.juneok.csv_import import preview_auction_csv
        market_before = market_status()
        try:
            preview_auction_csv(b"price,amount\n3000,9000000\n")
            checks["D06_bad_csv_keeps_active_data"] = False
        except ValueError:
            checks["D06_bad_csv_keeps_active_data"] = market_status() == market_before

        fault_ws = SharedWorkspace(AgentSession(Settings(api_key="", model="offline-demo"), SimulatedUnavailableClient()))
        fault_ws.load_demo()
        before = fault_ws.store.snapshot()
        reply = fault_ws.run("명시적으로 모의 장애를 주입하는 도구 점검입니다.")
        checks["D07_simulated_error_preserves_conditions"] = reply["status"] == "API_ERROR" and fault_ws.store.snapshot() == before
        recovery = fault_ws.calculate()
        checks["D07_keyless_calculation_recovers_3109"] = recovery["status"] == "OK" and recovery["options"][0]["minimum_whole_krw_per_kg"] == "3109"

    from io import BytesIO
    from pypdf import PdfReader
    first = PdfReader(BytesIO((folder / "01_기준조건_가상예시.pdf").read_bytes()))
    checks["D08_pdf_korean_and_numbers"] = "3,109원/kg" in "\n".join(p.extract_text() for p in first.pages)
    result = {"scope": "OFFLINE_HANDOFF_DEMO_NOT_LIVE_MODEL_OR_HUMAN_TEST", "created_at_kst": stamp,
              "live_model_requests": 0, "simulated_model_requests": reply.get("metrics", {}).get("model_requests", 0),
              "data_api_requests": 0, "http_blocked": True,
              "transport_failure": "EXPLICIT_SIMULATION", "checks": checks, "cases": cases,
              "passed": all(checks.values()), "output_dir": str(folder)}
    (folder / "시연검증결과.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROJECT_ROOT / "artifacts/handoff-demo/latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "checks": checks, "output_dir": str(folder)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
