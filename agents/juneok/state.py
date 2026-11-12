"""Versioned atomic scenario updates independent of the model provider."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from .comparison import TOP_FIELDS, validate_partial


def empty_scenario() -> dict:
    return {"quantity_kg": None, "current_price_per_kg": None, "price_basis": None,
            "current_fee_percent": None, "cost_policy": None, "included_costs": None,
            "current_costs": None, "storage_available": None, "shipping_flexible": None, "options": []}


def demo_scenario() -> dict:
    return {**empty_scenario(), "quantity_kg": 3000, "current_price_per_kg": 3000,
            "price_basis": "commission_net", "current_fee_percent": 0,
            "cost_policy": "equal_excluded", "included_costs": ["commission"],
            "storage_available": None, "shipping_flexible": None,
            "options": [{"id": "seven_days", "delay_days": 7, "loss_percent": 2,
                         "daily_storage_cost": 20000, "fee_percent": 0,
                         "future_price_per_kg": None, "costs": None, "loss_confirmed_for_period": True}]}


@dataclass
class ScenarioStore:
    scenario: dict = field(default_factory=empty_scenario)
    version: int = 0
    history: list[dict] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    origin_turn_id: str | None = None

    def __post_init__(self):
        self.scenario = {**empty_scenario(), **deepcopy(self.scenario)}
        self.history = deepcopy(self.history)
        self.provenance = deepcopy(self.provenance)
        validate_partial(self.scenario)
        for option in self.scenario["options"]:
            option["loss_confirmed_for_period"] = False

    def snapshot(self) -> dict:
        return {"version": self.version, "scenario": deepcopy(self.scenario), "provenance": deepcopy(self.provenance)}

    def update(self, patch: dict, expected_version: int, clear_fields: list[str] | None = None) -> dict:
        if type(expected_version) is not int or expected_version != self.version:
            return {"status": "INVALID_INPUT", "message": "시나리오 버전이 일치하지 않습니다.", "current": self.snapshot()}
        if not isinstance(patch, dict) or set(patch) - TOP_FIELDS:
            return {"status": "INVALID_INPUT", "message": "허용되지 않은 변경입니다."}
        clear_fields = [] if clear_fields is None else clear_fields
        if (not isinstance(clear_fields, list) or any(not isinstance(k, str) or k not in TOP_FIELDS - {"options"} for k in clear_fields)
                or len(set(clear_fields)) != len(clear_fields)
                or any(patch.get(k) is not None for k in clear_fields)):
            return {"status": "INVALID_INPUT", "message": "미확인으로 초기화할 필드를 확인하세요.", "current": self.snapshot()}
        candidate = deepcopy(self.scenario)
        changes = {k: deepcopy(v) for k, v in patch.items() if v is not None}
        candidate.update(changes)
        for name in clear_fields:
            candidate[name] = None
        old_options = {o["id"]: o for o in self.scenario["options"]}
        reset = []
        if isinstance(candidate.get("options"), list):
            for option in candidate["options"]:
                if not isinstance(option, dict):
                    continue
                old = old_options.get(option.get("id"))
                if old and old.get("delay_days") != option.get("delay_days") and option.get("loss_confirmed_for_period") is not True:
                    option["loss_percent"] = None
                    reset.append(option["id"])
        try:
            validate_partial(candidate)
        except (ValueError, TypeError) as exc:
            return {"status": "INVALID_INPUT", "message": str(exc), "current": self.snapshot()}
        # Confirmation is an assertion for this patch, never a persistent permission
        # to carry a loss rate to a different future period.
        for option in candidate["options"]:
            option["loss_confirmed_for_period"] = False
        self.history.append(self.snapshot())
        def flattened(scenario):
            values = {k: v for k, v in scenario.items() if k != "options"}
            values.update({f"options.{o['id']}.{k}": v for o in scenario["options"]
                           for k, v in o.items() if k not in {"id", "loss_confirmed_for_period"}})
            return values
        before, after = flattened(self.scenario), flattened(candidate)
        self.provenance = {k: v for k, v in self.provenance.items() if k in after}
        for name, value in after.items():
            if before.get(name) != value:
                reason = "period_change_requires_confirmation" if name in {f"options.{i}.loss_percent" for i in reset} else (
                    "explicitly_cleared" if name in clear_fields else "conversation_extraction" if self.origin_turn_id else "direct_input")
                self.provenance[name] = {"source": reason, "turn_id": self.origin_turn_id,
                                         "changed_at_version": self.version + 1}
        self.scenario = candidate
        self.version += 1
        return {"status": "OK", **self.snapshot(), "changed_fields": list(changes), "cleared_fields": clear_fields,
                "loss_reconfirmation_required": reset}
