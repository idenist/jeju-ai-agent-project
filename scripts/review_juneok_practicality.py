"""Reuse real scenario evidence; never grade synthetic runs as human field validation."""
from __future__ import annotations

import hashlib
import json
from agents.juneok.config import PROJECT_ROOT


def main():
    path = PROJECT_ROOT / "artifacts/evaluation-runs/20261109T040529628239.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = {r["id"]: r for r in data["runs"]}
    def result(case, name):
        entries = [x["result"] for x in cases[case]["turns"][-1]["tool_results"] if x["tool"] == name]
        return entries[-1] if entries else {}
    def scenario(case):
        return cases[case]["turns"][-1]["state"]["scenario"]
    checks = {
        "unknown_current_quote_not_invented": scenario("J11")["current_price_per_kg"] is None,
        "unknown_loss_not_zero_filled": scenario("J12")["options"][0]["loss_percent"] is None,
        "new_period_requires_new_loss": result("J03", "compare_shipment_options").get("status") == "NEEDS_INPUT",
        "unavailable_storage_is_execution_conflict": result("J19", "compare_shipment_options").get("feasibility", {}).get("status") == "CONFLICT",
        "future_market_date_not_invented": result("J15", "get_market_evidence").get("status") == "NO_DATA",
        "historical_context_has_seven_observed_years": len(result("J05", "get_historical_shipment_context").get("series", [])) == 7,
        "sensitivity_preserves_original_loss": scenario("J23")["options"][0]["loss_percent"] == 2,
        "missing_hypotheses_require_input": result("J24", "compare_sensitivity").get("status") == "NEEDS_INPUT",
        "no_future_price_grid_without_user_price": all(r["future_price_per_kg"] is None for r in result("J25", "compare_sensitivity").get("rows", [])),
    }
    mismatches = [p for p, value in data["source_sha256"].items() if hashlib.sha256((PROJECT_ROOT / p).read_bytes()).hexdigest() != value]
    report = {"scope": "PUBLIC_EVIDENCE_AND_SYNTHETIC_WORKFLOW_REVIEW", "scenario_checks": checks,
        "scenario_checks_passed": all(checks.values()), "source_mismatches": mismatches,
        "live_evaluation": path.relative_to(PROJECT_ROOT).as_posix(), "model_calls_in_this_review": 0,
        "human_participants": 0, "farmer_participants": 0, "farmer_effectiveness_verified": False,
        "public_workflow_assessment": "CONDITIONAL_USE_CASE_PLAUSIBILITY",
        "applicability_conditions": ["User can actually choose the shipment date.", "Actual quote and cost basis can be obtained.",
            "User supplies loss/price hypotheses or leaves them unknown.", "Storage and contract constraints are checked separately."],
        "not_established": ["Farmer input availability and burden", "Human comprehension and task time",
            "Benefit over phone/notes/Excel", "Real revenue or crop-quality improvement"]}
    folder = PROJECT_ROOT / "artifacts/public-practicality"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) and not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
