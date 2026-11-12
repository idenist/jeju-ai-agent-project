"""Outcome checks independent of the model's exact sequence of tool calls."""
from __future__ import annotations

from .reporting import render_report


def assess_turn(turn: dict) -> dict:
    visible = turn.get("visible_results", [])
    checks = {"user_response_present": bool(turn.get("text", "").strip()),
              "report_matches_visible_results": not visible or render_report(visible) == turn.get("report", ""),
              "no_stale_comparison": all(x["result"].get("scenario_version") == turn["state"]["version"]
                  for x in visible if x["tool"] in {"compare_shipment_options", "compare_sensitivity"} and x["result"].get("status") == "OK"),
              "delayed_options_only": all(type(o.get("delay_days")) is int and 1 <= o["delay_days"] <= 365
                  for o in turn.get("state", {}).get("scenario", {}).get("options", []) if o.get("delay_days") is not None)}
    return {"checks": checks, "passed": all(checks.values()),
            "model_requests": turn.get("metrics", {}).get("model_requests", 0),
            "tool_errors": sum(x["result"]["status"] in {"INVALID_INPUT", "DATA_ERROR"} for x in turn.get("tool_results", []))}
