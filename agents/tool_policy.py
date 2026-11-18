"""Explicit, reversible experiment: offer only A/B-related tools on clear A/B turns.

Default production behavior MUST keep its original complete tool catalog.
An explicit user/environment opt-in is required; test accuracy with live E2E
before any proposal to switch defaults. No language-model request is made here.
"""
from __future__ import annotations

AB_TOOL_NAMES = frozenset({
    "compare_future_pair", "analyze_shipment_timing",
    "get_price_history", "explain_term",
})


def select_offered_tools(tools: list[dict], *, enabled: bool,
                         future_pair_request: bool) -> tuple[list[dict], bool]:
    if not enabled or not future_pair_request:
        return tools, False
    reduced = [tool for tool in tools if tool.get("name") in AB_TOOL_NAMES]
    available = {tool.get("name") for tool in reduced}
    if not {"compare_future_pair", "analyze_shipment_timing"}.issubset(available):
        # Safety: do not silently remove all usable tools after schema changes.
        return tools, False
    return reduced, len(reduced) < len(tools)
