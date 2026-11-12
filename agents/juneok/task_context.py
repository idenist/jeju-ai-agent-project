"""Bounded per-case task selections, never a persistent user profile or price store."""
from copy import deepcopy
import hashlib
import json
import re
from uuid import uuid4

GOALS = {"review_case_changes": "저장 비교의 변경 검토", "explore_data": "확보 자료 탐색", "solve_offer_limits": "제안단가 허용 조건 검토", "compare_shipment_options": "출하 조건 비교", "update_scenario": "출하 조건 비교",
         "get_historical_shipment_context": "과거 출하자료 확인", "get_market_evidence": "동일규격 시장 참고", "get_price_history": "과거 경락가격 비교",
         "compare_sensitivity": "사용자 가정 범위 비교", "explain_decision_conditions": "판단 조건 설명",
         "build_review_brief": "자료와 조건의 종합 검토", "explain_term": "용어 확인"}
SHIPMENT_FIELDS = {"shipment_variety", "target_month", "start_year", "end_year"}
MARKET_FIELDS = {"market_code", "variety", "grade", "size", "package", "package_kg", "as_of"}
RANGE_FIELDS = {"option_id", "loss_values", "price_values", "delay_days"}


def data_revision():
    from .config import PROJECT_ROOT
    from .data_refresh import digest, resolve_market_root
    try:
        selected = resolve_market_root()
        manifest = PROJECT_ROOT / "data/metadata/juneok_manifest.json"
        pointer = PROJECT_ROOT / "artifacts/market-active.json"
        return (digest(manifest), digest(pointer) if pointer.exists() else "baseline", str(selected))
    except (OSError, ValueError, KeyError, TypeError):
        return ("DATA_UNAVAILABLE",)


def validate_context(value):
    keys = {"task_id", "version", "goal", "shipment_query", "market_query", "sensitivity_query", "pending"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("과업 맥락 필드가 다릅니다.")
    if not isinstance(value["task_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["task_id"]):
        raise ValueError("과업 식별자가 잘못됐습니다.")
    if type(value["version"]) is not int or value["version"] < 0 or value["goal"] not in {None, *GOALS.values()}:
        raise ValueError("과업 버전·목적이 잘못됐습니다.")
    for field, allowed in [("shipment_query", SHIPMENT_FIELDS), ("market_query", MARKET_FIELDS), ("sensitivity_query", RANGE_FIELDS)]:
        query = value[field]
        if query is None:
            continue
        if not isinstance(query, dict) or set(query) - allowed:
            raise ValueError("과업 조회조건이 잘못됐습니다.")
        for key, item in query.items():
            if key in {"loss_values", "price_values"}:
                if item is not None:
                    from .comparison import decimal
                    if not isinstance(item, list) or not 1 <= len(item) <= 7:
                        raise ValueError("과업 가정 목록이 잘못됐습니다.")
                    for number in item:
                        parsed = decimal(number, key)
                        if parsed < 0 or (key == "loss_values" and parsed >= 100):
                            raise ValueError("과업 가정 범위가 잘못됐습니다.")
            elif key in {"target_month", "start_year", "end_year", "delay_days"}:
                maximum = 12 if key == "target_month" else 365 if key == "delay_days" else 9999
                if item is not None and (type(item) is not int or not 1 <= item <= maximum):
                    raise ValueError("과업 조회기간이 잘못됐습니다.")
            elif key == "package_kg":
                from .comparison import decimal
                if item is not None and decimal(item, key) <= 0:
                    raise ValueError("과업 포장중량이 잘못됐습니다.")
            elif key == "as_of" and item is not None:
                from datetime import date
                if not isinstance(item, str) or date.fromisoformat(item).isoformat() != item:
                    raise ValueError("과업 기준일이 잘못됐습니다.")
            elif item is not None and (not isinstance(item, str) or not 1 <= len(item) <= 200):
                raise ValueError("과업 조회조건 문자열을 확인하세요.")
    pending = value["pending"]
    if not isinstance(pending, dict) or set(pending) - set(GOALS):
        raise ValueError("과업 미확인 항목이 잘못됐습니다.")
    if any(not isinstance(items, list) or len(items) > 40 or any(not isinstance(x, str) or len(x) > 200 for x in items) for items in pending.values()):
        raise ValueError("과업 미확인 항목이 너무 큽니다.")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 12000:
        raise ValueError("과업 맥락이 너무 큽니다.")


class TaskContext:
    def __init__(self, snapshot=None):
        value = snapshot if snapshot is not None else {"task_id": uuid4().hex, "version": 0, "goal": None,
            "shipment_query": None, "market_query": None, "sensitivity_query": None, "pending": {}}
        validate_context(value)
        self.value = deepcopy(value)

    def snapshot(self):
        return deepcopy(self.value)

    def reconcile_scenario(self, scenario):
        query = self.value["sensitivity_query"]
        if query and (query.get("loss_values") or query.get("price_values")):
            option = next((o for o in scenario.get("options", []) if o["id"] == query.get("option_id")), None)
            if option is None or option.get("delay_days") != query.get("delay_days"):
                self.value["sensitivity_query"] = None
                self.value["version"] += 1

    def record(self, name, args, result, set_goal=True, scenario=None):
        if scenario is not None:
            self.reconcile_scenario(scenario)
        if name not in GOALS or not isinstance(result, dict):
            return
        candidate = self.snapshot()
        status = result.get("status")
        if status not in {"OK", "NEEDS_INPUT", "NEEDS_CLARIFICATION", "NO_DATA"}:
            return
        if set_goal:
            candidate["goal"] = GOALS[name]
        if name == "explore_data":
            filters = {k: v for k, v in (args.get("filters") or {}).items() if v is not None}
            if args.get("source") == "market":
                candidate["market_query"] = filters
            else:
                candidate["shipment_query"] = {k: v for k, v in filters.items() if k != "year"}
                if filters.get("year") is not None:
                    candidate["shipment_query"].update(start_year=filters["year"], end_year=filters["year"])
        if name == "get_historical_shipment_context":
            candidate["shipment_query"] = {k: deepcopy(v) for k, v in args.items() if k in SHIPMENT_FIELDS}
        elif name == "get_market_evidence":
            candidate["market_query"] = {k: deepcopy(v) for k, v in args.items() if k in MARKET_FIELDS}
        elif name in {"compare_sensitivity", "explain_decision_conditions"}:
            # No list means explain saved conditions, rather than reuse an old range silently.
            candidate["sensitivity_query"] = {k: deepcopy(v) for k, v in args.items() if k in RANGE_FIELDS}
        elif name == "build_review_brief":
            for field in ["shipment_query", "market_query"]:
                candidate[field] = deepcopy(args.get(field))
            candidate["sensitivity_query"] = {k: deepcopy(args.get(k)) for k in RANGE_FIELDS}
        if name in {"compare_sensitivity", "explain_decision_conditions", "build_review_brief"} and scenario is not None:
            query = candidate["sensitivity_query"]
            options = scenario.get("options", [])
            if query.get("option_id") is None and len(options) == 1:
                query["option_id"] = options[0]["id"]
            option = next((o for o in options if o["id"] == query.get("option_id")), None)
            query["delay_days"] = option.get("delay_days") if option else None
        missing = result.get("missing_fields", [])
        if isinstance(missing, list) and missing:
            candidate["pending"][name] = deepcopy(missing)
        else:
            candidate["pending"].pop(name, None)
        if name == "build_review_brief":
            for component in result.get("components", []):
                tool, output = component["tool"], component["result"]
                if output.get("missing_fields"):
                    candidate["pending"][tool] = deepcopy(output["missing_fields"])
                else:
                    candidate["pending"].pop(tool, None)
        # Bad/oversize tool selections may be displayed, but never break the harness's memory.
        try:
            validate_context(candidate)
        except (ValueError, TypeError):
            return
        if candidate != self.value:
            candidate["version"] += 1
            self.value = candidate

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
