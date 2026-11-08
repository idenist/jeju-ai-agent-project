"""One versioned workspace for conversational and keyless card interactions."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from .agent import AgentSession
from .state import demo_scenario
from .tools import execute_tool
from .ui_workflow import INPUT_CONTEXTS, COST_LABELS, manual_scenario
from .reporting import FIELD_NAMES
from .config import PROJECT_ROOT


def data_revision():
    """Detect shared CSV selection changes, including changes made by another UI/CLI."""
    from .data_refresh import digest, resolve_market_root
    try:
        selected = resolve_market_root()
        manifest = PROJECT_ROOT / "data/metadata/juneok_manifest.json"
        pointer = PROJECT_ROOT / "artifacts/market-active.json"
        return (digest(manifest), digest(pointer) if pointer.exists() else "baseline", str(selected))
    except (OSError, ValueError, KeyError, TypeError):
        return ("DATA_UNAVAILABLE",)


def card_values(snapshot: dict) -> tuple[dict, list[str]]:
    scenario = snapshot["scenario"]
    def text(value):
        return "" if value is None else str(value)
    names = {v: k for k, v in COST_LABELS.items()}
    options = scenario.get("options", [])
    values = {"quantity": text(scenario.get("quantity_kg")), "price": text(scenario.get("current_price_per_kg")),
        "basis": {"commission_net": "수수료 차감 후 단가", "gross": "수수료 차감 전 단가"}.get(scenario.get("price_basis"), "미확인"),
        "policy": {"equal_excluded": "보관비 외 기타 고정비용은 양쪽 동일하여 제외", "itemized": "대안별 비용을 항목별 입력"}.get(scenario.get("cost_policy"), "미확인"),
        "included": [names[x] for x in scenario.get("included_costs") or []],
        "included_confirmed": scenario.get("included_costs") is not None,
        "current_fee": text(scenario.get("current_fee_percent")), "option_count": max(1, len(options))}
    for key in ["storage_available", "shipping_flexible"]:
        value = scenario.get(key)
        values[key] = "예" if value is True else "아니오" if value is False else "미확인"
    def costs(prefix, items):
        for category in COST_LABELS.values():
            if category != "commission":
                values[prefix + "_" + category] = next((text(x["amount_krw"]) for x in items or [] if x["category"] == category), "")
        values[prefix + "_none"] = items == []
    costs("current", scenario.get("current_costs"))
    ids = []
    for i in range(3):
        option = options[i] if i < len(options) else {}
        ids.append(option.get("id", f"카드 대안 {i + 1}"))
        for widget, field in [("days", "delay_days"), ("loss", "loss_percent"), ("storage", "daily_storage_cost"),
                              ("future", "future_price_per_kg"), ("fee", "fee_percent")]:
            values[f"{widget}_{i}"] = option.get(field) if widget == "days" else text(option.get(field))
        # The store consumes the assertion flag; a saved loss value remains valid for its saved period.
        values[f"period_{i}"] = option.get("loss_percent") is not None
        costs(f"option_{i}", option.get("costs"))
    # A model-provided identifier must never collide with a generated new card identifier.
    for i in range(len(options), 3):
        while ids[i] in ids[:i]:
            ids[i] += "+"
    return values, ids


def preserve_equivalent_numbers(candidate, previous):
    """A text widget round trip must not mark unchanged numeric values as new facts."""
    def preserve(target, old, fields):
        for key in fields:
            new, prior = target.get(key), old.get(key)
            if new is None or prior is None:
                continue
            try:
                if Decimal(str(new)).is_finite() and Decimal(str(new)) == Decimal(str(prior)):
                    target[key] = prior
            except (ValueError, ArithmeticError):
                pass
    def costs(items, old_items):
        old = {x["category"]: x for x in old_items or []}
        for item in items or []:
            preserve(item, old.get(item["category"], {}), ["amount_krw"])
        if isinstance(items, list) and isinstance(old_items, list) and len(items) == len(old_items):
            new_map = {x["category"]: x["amount_krw"] for x in items}
            old_map = {x["category"]: x["amount_krw"] for x in old_items}
            if len(new_map) == len(items) and new_map == old_map:
                items[:] = deepcopy(old_items)
    preserve(candidate, previous, ["quantity_kg", "current_price_per_kg", "current_fee_percent"])
    if candidate.get("included_costs") is not None and previous.get("included_costs") is not None:
        if set(candidate["included_costs"]) == set(previous["included_costs"]):
            candidate["included_costs"] = deepcopy(previous["included_costs"])
    costs(candidate.get("current_costs"), previous.get("current_costs"))
    old_options = {o["id"]: o for o in previous.get("options", [])}
    for option in candidate["options"]:
        old = old_options.get(option["id"], {})
        preserve(option, old, ["delay_days", "loss_percent", "daily_storage_cost", "fee_percent", "future_price_per_kg"])
        costs(option.get("costs"), old.get("costs"))


class SharedWorkspace:
    def __init__(self, session: AgentSession):
        self.session = session
        self.input_context = INPUT_CONTEXTS[0]
        self.entries: list[dict] = []
        self.result_version: int | None = None
        self.data_revision = data_revision()
        self.result_data_revision = None

    @property
    def store(self):
        return self.session.store

    def _freeze(self, entries, channel, changed_fields=None, execution_status="OK", expected_data_revision=None):
        self.entries = deepcopy(entries)
        if execution_status != "OK":
            self.entries.append({"tool": "execution_status", "result": {"status": "ERROR",
                "message": f"이번 요청은 {execution_status} 상태로 끝났습니다. 아래 조회·계산은 확인된 부분 결과이며 요청 전체가 완료된 것은 아닙니다."}})
        for entry in self.entries:
            context = self.input_context
            if entry["tool"] in {"get_market_evidence", "get_historical_shipment_context"}:
                context = "원출처 관측자료 조회 · 비교 조건의 가정과 별개"
            elif entry["tool"] == "explain_term":
                context = "용어 설명 · 실제 농가 조건의 사실 검증 아님"
            entry["result"]["ui_context"] = {"version": self.store.version, "input_context": self.input_context,
                "channel": channel, "changed_fields": changed_fields or []}
            entry["result"]["ui_context"]["input_context"] = context
            entry["result"]["ui_context"]["kind"] = "evidence" if entry["tool"] in {"get_market_evidence", "get_historical_shipment_context", "explain_term"} else "comparison"
        self.result_version = self.store.version
        self.result_data_revision = expected_data_revision if expected_data_revision is not None else data_revision()
        return deepcopy(self.entries)

    def current_entries(self):
        return deepcopy(self.entries) if self.result_version == self.store.version and self.result_data_revision == data_revision() else []

    def check_data_change(self):
        current = data_revision()
        if current == self.data_revision:
            return False
        self.clear_conversation(preserve_conditions=True)
        self.data_revision = current
        return True

    def calculate(self, changed_fields=None):
        revision = data_revision()
        result = execute_tool("compare_shipment_options", {"expected_version": self.store.version}, self.store)
        self._freeze([{"tool": "compare_shipment_options", "result": result}], "공유 조건", changed_fields, expected_data_revision=revision)
        return result

    def sensitivity(self, option_id, losses, prices):
        revision = data_revision()
        result = execute_tool("compare_sensitivity", {"expected_version": self.store.version,
            "option_id": option_id, "loss_values": losses, "price_values": prices}, self.store)
        entries = [x for x in self.current_entries() if x["tool"] != "compare_sensitivity"]
        self._freeze([*entries, {"tool": "compare_sensitivity", "result": result}], "공유 조건", expected_data_revision=revision)
        return result

    def history(self, args):
        revision = data_revision()
        result = execute_tool("get_historical_shipment_context", args, self.store)
        self._freeze([{"tool": "get_historical_shipment_context", "result": result}], "과거 출하자료 조회", expected_data_revision=revision)
        return result

    def apply_card(self, values: dict, expected_version: int, option_ids: list[str]):
        if expected_version != self.store.version:
            return {"status": "INVALID_INPUT", "message": "카드의 기준 조건이 오래됐습니다. 현재 조건을 다시 불러오세요."}
        scenario = manual_scenario(values)
        for i, option in enumerate(scenario["options"]):
            option["id"] = option_ids[i]
        preserve_equivalent_numbers(scenario, self.store.scenario)
        clear = [key for key, value in scenario.items() if key != "options" and value is None]
        before = deepcopy(self.store.scenario)
        self.store.origin_turn_id = None
        updated = self.store.update(scenario, expected_version, clear)
        if updated["status"] == "OK":
            # This is a user's description of the input, never a claim of external verification.
            self.input_context = values.get("input_context", INPUT_CONTEXTS[0])
            changes = [FIELD_NAMES.get(k, k) for k, value in self.store.scenario.items()
                       if k != "options" and before.get(k) != value]
            old_options = {o["id"]: o for o in before.get("options", [])}
            for i, option in enumerate(self.store.scenario["options"]):
                old = old_options.get(option["id"], {})
                changes.extend(f"대안 {i + 1} {FIELD_NAMES.get(k, k)}" for k, value in option.items()
                               if k not in {"id", "loss_confirmed_for_period"} and old.get(k) != value)
            if {o["id"] for o in before.get("options", [])} != {o["id"] for o in self.store.scenario["options"]}:
                changes.append("비교할 보관 대안")
            self.calculate(changes)
        return updated

    def load_demo(self):
        scenario = demo_scenario()
        self.store.origin_turn_id = None
        updated = self.store.update(scenario, self.store.version,
            [k for k, v in scenario.items() if k != "options" and v is None])
        if updated["status"] == "OK":
            self.input_context = INPUT_CONTEXTS[2]
            self.calculate([FIELD_NAMES.get(k, k) for k in updated.get("changed_fields", [])])
        return updated

    def run(self, question):
        revision = data_revision()
        reply = self.session.run(question)
        self._freeze(reply.get("visible_results", []), "대화와 공유 조건", execution_status=reply.get("status", "ERROR"), expected_data_revision=revision)
        return reply

    def clear_conversation(self, preserve_conditions=True):
        self.session.messages = []
        self.entries = []
        self.result_version = None
        if not preserve_conditions:
            from .state import ScenarioStore
            self.session.store = ScenarioStore()
            self.input_context = INPUT_CONTEXTS[0]
