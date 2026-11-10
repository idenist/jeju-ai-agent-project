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
from .task_context import TaskContext, data_revision
from .harness import FAILURE_GUIDANCE


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
        from .settlement import ShipmentStore
        self.shipment = ShipmentStore()
        self.session.shipment = self.shipment

    @property
    def store(self):
        return self.session.store

    def _freeze(self, entries, channel, changed_fields=None, execution_status="OK", expected_data_revision=None):
        self.entries = deepcopy(entries)
        if execution_status != "OK":
            self.entries.append({"tool": "execution_status", "result": {"status": "ERROR",
                "reason": execution_status,
                "message": FAILURE_GUIDANCE.get(execution_status, FAILURE_GUIDANCE["API_ERROR"]) + " 아래 조회·계산은 확인된 부분 결과이며 요청 전체가 완료된 것은 아닙니다."}})
        for entry in self.entries:
            context = self.input_context
            if entry["tool"] in {"get_market_evidence", "get_historical_shipment_context", "get_price_history", "explore_data"}:
                context = "원출처 관측자료 조회 · 비교 조건의 가정과 별개"
            elif entry["tool"] == "explain_term":
                context = "용어 설명 · 실제 농가 조건의 사실 검증 아님"
            entry["result"]["ui_context"] = {"version": self.store.version, "input_context": self.input_context,
                "task_id": self.session.task_context.snapshot()["task_id"],
                "task_context_version": self.session.task_context.snapshot()["version"],
                "channel": channel, "changed_fields": changed_fields or []}
            entry["result"]["ui_context"]["input_context"] = context
            entry["result"]["ui_context"]["kind"] = "evidence" if entry["tool"] in {"get_market_evidence", "get_historical_shipment_context", "get_price_history", "explore_data", "explain_term"} else "comparison"
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
        self.session.task_context = TaskContext()
        if hasattr(self, "restored_archive"):
            del self.restored_archive
        self.data_revision = current
        return True

    def calculate(self, changed_fields=None, preserve_task_goal=False):
        revision = data_revision()
        result = execute_tool("compare_shipment_options", {"expected_version": self.store.version}, self.store)
        self.session.task_context.record("compare_shipment_options", {}, result, set_goal=not preserve_task_goal, scenario=self.store.scenario)
        self._freeze([{"tool": "compare_shipment_options", "result": result}], "공유 조건", changed_fields, expected_data_revision=revision)
        return result

    def sensitivity(self, option_id, losses, prices):
        revision = data_revision()
        result = execute_tool("compare_sensitivity", {"expected_version": self.store.version,
            "option_id": option_id, "loss_values": losses, "price_values": prices}, self.store)
        self.session.task_context.record("compare_sensitivity", {"option_id": option_id, "loss_values": losses, "price_values": prices}, result, scenario=self.store.scenario)
        entries = [x for x in self.current_entries() if x["tool"] != "compare_sensitivity"]
        self._freeze([*entries, {"tool": "compare_sensitivity", "result": result}], "공유 조건", expected_data_revision=revision)
        return result

    def history(self, args):
        revision = data_revision()
        result = execute_tool("get_historical_shipment_context", args, self.store)
        self.session.task_context.record("get_historical_shipment_context", args, result, scenario=self.store.scenario)
        self._freeze([{"tool": "get_historical_shipment_context", "result": result}], "과거 출하자료 조회", expected_data_revision=revision)
        return result

    def explore(self, source, filters=None, offset=0, limit=20):
        revision = data_revision()
        args = {"source": source, "filters": filters, "offset": offset, "limit": limit}
        result = execute_tool("explore_data", args, self.store)
        self.session.task_context.record("explore_data", args, result, scenario=self.store.scenario)
        self._freeze([{"tool": "explore_data", "result": result}], "확보 자료 탐색", expected_data_revision=revision)
        return result

    def offer_limits(self, option_id, quote, solve_for):
        revision = data_revision()
        args = {"expected_version": self.store.version, "option_id": option_id, "quoted_price_per_kg": quote, "solve_for": solve_for}
        result = execute_tool("solve_offer_limits", args, self.store)
        self.session.task_context.record("solve_offer_limits", args, result, scenario=self.store.scenario)
        self._freeze([{"tool": "solve_offer_limits", "result": result}], "제안단가 허용 조건", expected_data_revision=revision)
        return result

    def market(self, args):
        revision = data_revision()
        result = execute_tool("get_market_evidence", args, self.store)
        self.session.task_context.record("get_market_evidence", args, result, scenario=self.store.scenario)
        self._freeze([{"tool": "get_market_evidence", "result": result}], "선택한 시장 참고", expected_data_revision=revision)
        return result

    def review(self, **args):
        revision = data_revision()
        result = execute_tool("build_review_brief", {"expected_version": self.store.version, **args}, self.store)
        self.session.task_context.record("build_review_brief", args, result, scenario=self.store.scenario)
        from .presentation import select_results
        self._freeze(select_results([{"tool": "build_review_brief", "result": result}], self.store.version),
                     "출하 검토", expected_data_revision=revision)
        return result

    def explain_conditions(self, option_id=None, loss_values=None, price_values=None):
        revision = data_revision()
        result = execute_tool("explain_decision_conditions", {"expected_version": self.store.version,
            "option_id": option_id, "loss_values": loss_values, "price_values": price_values}, self.store)
        self.session.task_context.record("explain_decision_conditions", {"option_id": option_id, "loss_values": loss_values, "price_values": price_values}, result, scenario=self.store.scenario)
        entries = [e for e in self.current_entries() if e["tool"] != "explain_decision_conditions"]
        self._freeze([*entries, {"tool": "explain_decision_conditions", "result": result}], "판단 조건 설명", expected_data_revision=revision)
        return result

    def export_case(self, name):
        from .casefiles import export_case
        return export_case(self, name)

    def compare_case(self, content, expected_version):
        from .casefiles import parse_case
        if type(expected_version) is not int or expected_version != self.store.version:
            return {"status": "INVALID_INPUT", "message": "변경 검토 기준 조건이 바뀌었습니다. 다시 확인하세요."}
        try: saved = parse_case(content)
        except ValueError as exc: return {"status": "INVALID_INPUT", "message": str(exc)}
        revision = data_revision()
        result = execute_tool("review_case_changes", {"expected_version": expected_version}, self.store,
            reference_case=saved, task_context=self.session.task_context.snapshot(), input_context=self.input_context)
        if result["status"] == "OK":
            self.session.comparison_reference = saved
            self.session.comparison_input_context = self.input_context
            self.session.task_context.record("review_case_changes", {}, result, scenario=self.store.scenario)
            self._freeze([{"tool": "review_case_changes", "result": result}], "저장 비교 변경 검토", expected_data_revision=revision)
        return result

    def restore_case(self, content, expected_version):
        from .casefiles import parse_case
        if type(expected_version) is not int or expected_version != self.store.version:
            return {"status": "INVALID_INPUT", "message": "복원 기준 조건이 바뀌었습니다. 파일을 다시 확인하세요."}
        try:
            saved = parse_case(content)
        except ValueError as exc:
            return {"status": "INVALID_INPUT", "message": str(exc)}
        candidate = deepcopy(saved["snapshot"]["scenario"])
        for option in candidate["options"]:
            option["loss_confirmed_for_period"] = option.get("loss_percent") is not None
        self.store.origin_turn_id = None
        result = self.store.update(candidate, expected_version, [k for k, v in candidate.items() if k != "options" and v is None])
        if result["status"] != "OK":
            return result
        self.clear_conversation(preserve_conditions=True)
        self.input_context = saved["input_context"]
        self.session.task_context = TaskContext(saved.get("task_context"))
        self.data_revision = data_revision()
        self.restored_archive = saved
        self.session.comparison_reference = None
        self.store.history = deepcopy([*saved["history"], saved["snapshot"]][-100:])
        for value in self.store.provenance.values():
            value.update(source="imported_user_input_not_verified", turn_id=None, changed_at_version=self.store.version)
        self.calculate(["저장한 비교 조건 복원 · 현재 자료로 재계산"], preserve_task_goal=True)
        return {"status": "OK", "name": saved["name"], "data_changed": saved["data_revision"] != list(self.data_revision),
                "message": "조건을 복원하고 현재 자료 기준으로 재계산했습니다. 당시 결과는 보관 기록이며 현재 결과로 실행하지 않습니다."}

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
            self.clear_conversation(preserve_conditions=True)
            self.session.task_context = TaskContext()
            if hasattr(self, "restored_archive"):
                del self.restored_archive
            self.session.comparison_reference = None
            self.input_context = INPUT_CONTEXTS[2]
            self.calculate([FIELD_NAMES.get(k, k) for k in updated.get("changed_fields", [])])
        return updated

    def run(self, question):
        revision = data_revision()
        self.session.comparison_input_context = self.input_context
        reply = self.session.run(question)
        self._freeze(reply.get("visible_results", []), "대화와 공유 조건", execution_status=reply.get("status", "ERROR"), expected_data_revision=revision)
        return reply

    def clear_conversation(self, preserve_conditions=True):
        self.session.messages = []
        self.session.price_history_query = None
        self.session.price_history_revision = None
        self.entries = []
        self.result_version = None
        if not preserve_conditions:
            self.shipment.clear()
            self.session.comparison_reference = None
            from .state import ScenarioStore
            self.session.store = ScenarioStore()
            self.session.task_context = TaskContext()
            if hasattr(self, "restored_archive"):
                del self.restored_archive
            self.input_context = INPUT_CONTEXTS[0]
