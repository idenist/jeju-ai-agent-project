"""Read-only alignment and export of Hub #855 evidence with verified A/B economics.

Never invent a forecast or attach an economic calculation to another pair of dates.
The existing jun-eok economic model and report renderer are used as-is.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from services.shipment_timing import DAILY_PATH, ROOT

FORMAT = "jeju-hub855-shipment-timing-review"


def aligned_economic(timing: dict, candidate: dict | None) -> dict | None:
    """Attach only a successful economic comparison for exactly the same sale dates."""
    if not isinstance(timing, dict) or not isinstance(candidate, dict):
        return None
    if timing.get("status") not in {"OK", "INSUFFICIENT_DATA"}:
        return None
    if candidate.get("status") != "OK" or candidate.get("analysis_type") != "future_pair":
        return None
    query = timing.get("input")
    if not isinstance(query, dict):
        return None
    try:
        option_a = candidate["option_a"]["sale_date"]
        options_b = candidate["options_b"]
        if not isinstance(options_b, list) or not options_b:
            return None
        sale_dates = {option["sale_date"] for option in options_b}
        if (option_a != query["option_a_date"] or
                sale_dates != {query["option_b_date"]}):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return candidate


def report_entries(timing: dict, economic: dict | None = None) -> list[dict]:
    """Identical canonical input for the on-screen markdown and the PDF exporter."""
    linked = aligned_economic(timing, economic)
    entries = []
    if linked is not None:
        entries.append({"tool": "compare_future_pair", "result": linked})
    entries.append({"tool": "analyze_shipment_timing", "result": timing})
    return entries


def export_payload(timing: dict, economic: dict | None = None) -> dict:
    """Machine-readable artifact; no hidden estimates or implicit variety mapping."""
    linked = aligned_economic(timing, economic)
    return {
        "format": FORMAT,
        "version": 1,
        "status": ("INTEGRATED" if linked else
                   "SHIPMENT_ONLY" if timing.get("status") == "OK" else "PARTIAL"),
        "same_date_economic_result_included": linked is not None,
        "is_future_forecast": False,
        "shipment_analysis": timing,
        "economic_comparison": linked,
        "limitations": [
            "제주데이터허브 #855는 과거 신고 출하량이며 현재 수급이나 미래가격 예측이 아닙니다.",
            "경제성 계산이 포함된 경우에도 단가·감모·보관비 등의 사용자 입력 가정만 적용합니다.",
            "경매 품종과 제주데이터허브 출하통계 품종은 자동 매핑하지 않습니다.",
            "두 결과의 A/B 출하일이 다르면 경제성 계산은 통합하지 않습니다.",
        ],
    }


def export_json(timing: dict, economic: dict | None = None) -> str:
    return json.dumps(export_payload(timing, economic), ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False)


def shipment_source_is_current(source_sha: str | None, root: Path | None = None) -> bool:
    """Reject historical reports after the daily shipment file changes."""
    if not isinstance(source_sha, str) or len(source_sha) != 64:
        return False
    path = (Path(root) if root is not None else ROOT) / DAILY_PATH
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == source_sha
    except OSError:
        return False


def source_fingerprint(root: Path | None = None) -> tuple[str | None, str | None]:
    """Content hashes, not modification time, to invalidate cached UI reports."""
    from services.shipment_timing import REPORT_PATH
    base = Path(root) if root is not None else ROOT
    hashes = []
    for rel in (DAILY_PATH, REPORT_PATH):
        try:
            hashes.append(hashlib.sha256((base / rel).read_bytes()).hexdigest())
        except OSError:
            hashes.append(None)
    return tuple(hashes)


def json_from_entries(entries: list[dict]) -> str:
    """Export exactly what a combined PDF report will contain."""
    timing = next((x["result"] for x in entries if x["tool"] == "analyze_shipment_timing"), None)
    economic = next((x["result"] for x in entries if x["tool"] == "compare_future_pair"), None)
    if timing is None:
        raise ValueError("제주데이터허브 출하량 분석 결과가 없습니다.")
    return export_json(timing, economic)
