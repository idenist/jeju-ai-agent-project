"""Join an existing A/B calculation to same-spec historical auction evidence.

A historical daily mean exceeding a threshold is NOT a probability of obtaining
that price, and the two price bases can still differ in actual selling costs.
No stored comparison state or source CSV is modified here.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from services.seasonal_prices import FILTERS, PROJECT_ROOT
from services.seasonal_prices import compare_break_even_to_history


def _error(status, message):
    return {"analysis_type": "market_decision", "status": status, "message": message}


def _positive_decimal(value):
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError("유효한 손익분기 가격이 필요합니다.") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("손익분기 가격은 0보다 커야 합니다.")
    return number


def market_spec_identity(conditions):
    """Normalize the chosen product for comparison across requests and renders."""
    if not isinstance(conditions, dict):
        return None
    values = {key: str(conditions.get(key) or "").strip() for key in FILTERS}
    if not all(values.values()):
        return None
    try:
        values["package_kg"] = str(_positive_decimal(values["package_kg"]).normalize())
    except ValueError:
        return None
    return tuple(values[key] for key in FILTERS)


def market_query_from_pair_result(pair_result):
    """Read the CSV-verified trade specification actually used for the B scenarios."""
    options = pair_result.get("options_b") or []
    evidence = pair_result.get("evidence") or {}
    if not isinstance(options, list) or not options or not isinstance(options[0], dict) or not isinstance(evidence, dict):
        return None
    option = evidence.get(options[0].get("id"))
    conditions = option.get("conditions") if isinstance(option, dict) else None
    if market_spec_identity(conditions) is None:
        return None
    return {key: conditions[key] for key in FILTERS}


def market_query_from_explicit_selection(seasonal_result):
    """Reuse only a *user-submitted* seasonal lookup, never model-guessed specs."""
    if not isinstance(seasonal_result, dict) or seasonal_result.get("status") not in (
        "OK", "PARTIAL_DATA", "NO_DATA"
    ):
        return None
    conditions = seasonal_result.get("conditions")
    if market_spec_identity(conditions) is None:
        return None
    return {**{k: conditions[k] for k in FILTERS},
            "window_days": conditions.get("window_days", 7)}


def threshold_observation_eligible(pair_result):
    """Allow a historical observation only for a complete gross threshold with no B price."""
    if not isinstance(pair_result, dict) or (
        pair_result.get("status") != "OK"
        or pair_result.get("analysis_type") != "future_pair"
        or pair_result.get("price_basis") != "gross"
        or pair_result.get("threshold_only") is not True
        or pair_result.get("storage_cost_range") is not None
        or pair_result.get("unknown_cost_fields") != []
    ):
        return False
    options = pair_result.get("options_b")
    if not isinstance(options, list) or len(options) != 1 or not isinstance(options[0], dict):
        return False
    return all(key in options[0] and options[0][key] is None for key in (
        "price_per_kg", "proceeds_krw", "difference_vs_a_krw"
    ))


def evaluate_market_decision(pair_result: dict, selection: dict | None,
                             *, root: Path = PROJECT_ROOT,
                             allow_threshold_observation: bool = False) -> dict:
    """Build a machine-readable, exportable joined result without guessing specs."""
    if not isinstance(pair_result, dict) or pair_result.get("status") != "OK" or (
        pair_result.get("analysis_type") != "future_pair"
    ):
        return _error("NO_PAIR", "먼저 두 미래 출하안의 손익을 계산해 주세요.")
    if pair_result.get("price_basis") != "gross":
        return _error("NOT_COMPARABLE", "수수료 차감 후 단가와 차감 전 경락가격은 직접 비교할 수 없습니다.")
    threshold_observation = allow_threshold_observation and threshold_observation_eligible(pair_result)
    if (pair_result.get("threshold_only") or pair_result.get("suppress_market_evidence")) and not threshold_observation:
        return _error("NOT_APPLICABLE", "현재 B안에는 다년도 시장 근거를 연결하지 않습니다.")
    selection_id = market_spec_identity(selection)
    if selection_id is None:
        return _error("NEEDS_SPEC", "시장·품종·등급·크기·포장·상자중량·거래방식을 확인해 주세요.")
    pair_spec = market_query_from_pair_result(pair_result)
    if pair_spec and selection_id != market_spec_identity(pair_spec):
        return _error("NEEDS_SPEC", "현재 B안에 적용한 거래규격으로 시장 근거를 다시 확인해 주세요.")
    if selection.get("trade_type") != "경매":
        return _error("NOT_COMPARABLE", "이 분석은 경매 거래만 지원합니다. 정가수의 거래는 섞지 않습니다.")
    try:
        threshold = _positive_decimal(pair_result["minimum_whole_krw_per_kg"])
        options = pair_result["options_b"]
        target = date.fromisoformat(options[0]["sale_date"])
        option_a_date = date.fromisoformat(pair_result["option_a"]["sale_date"])
        if len({o["sale_date"] for o in options}) != 1:
            return _error("INVALID_INPUT", "B안 가격 가정들의 출하 예정일이 서로 다릅니다.")
        query = {k: selection[k] for k in FILTERS}
        query.update(month_day=target.strftime("%m-%d"), window_days=selection.get("window_days", 7))
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        return _error("INVALID_INPUT", str(exc) or "B안 출하일과 손익분기 가격을 확인해 주세요.")

    analysis = compare_break_even_to_history(query, str(threshold), price_basis="gross", root=root)
    if analysis.get("status") not in ("OK", "NO_CLEAN_DAYS", "NO_DATA", "DATA_ERROR"):
        return {"analysis_type": "market_decision", "status": analysis.get("status", "DATA_ERROR"),
                "message": analysis.get("message", "과거 시장가격 비교를 완료하지 못했습니다."),
                "threshold_krw_per_kg": str(threshold), "query": query}

    only_manual = len(options) == 1 and options[0].get("id") == "manual_b"
    difference = None
    if only_manual and options[0].get("difference_vs_a_krw") is not None:
        try:
            difference = Decimal(str(options[0]["difference_vs_a_krw"]))
            if not difference.is_finite():
                difference = None
        except (InvalidOperation, ValueError):
            difference = None
    decision = ("B_ASSUMED_HIGHER" if difference is not None and difference > 0 else
                "A_ASSUMED_HIGHER" if difference is not None and difference < 0 else
                "ASSUMED_TIE" if difference == 0 else "SCENARIOS_NOT_RANKED")
    return {"analysis_type": "market_decision", "status": analysis["status"],
            "comparison": {"option_a_date": option_a_date.isoformat(),
                           "option_b_date": target.isoformat(),
                           "minimum_whole_krw_per_kg": str(threshold),
                           **({"threshold_only": True} if threshold_observation else {}),
                           "difference_vs_a_krw": str(difference) if difference is not None else None,
                           "decision_under_assumptions": decision},
            "query": query,
            **({"message": analysis["message"]} if "message" in analysis else {}),
            "spec_confirmation": "사용자가 조회 또는 직접 입력한 거래규격(실제 출하 상품과의 일치 여부는 미확인)",
            "market_evidence": analysis,
            "warnings": [
                "과거 동일 규격의 일별 평균 경락가격을 비교한 것이며 미래가격 달성 확률이 아닙니다.",
                "사용자의 가정 판매단가는 경락가격과 공제·계약 조건이 다를 수 있습니다.",
            ]}
