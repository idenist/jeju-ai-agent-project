"""Step 1: deterministic, short partial answers; no guessed economic inputs."""
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from agents.juneok.progressive_dialogue import (
    MENU_FUTURE, MENU_NOW, current_comparison_guidance, future_pair_guidance, quickstart_reply,
)


class ProgressiveDialogueTests(unittest.TestCase):
    def test_menu_phrases_stable(self):
        self.assertEqual(MENU_NOW, "감귤을 지금 팔 때와 더 보관하고 팔 때를 비교해 주세요.")
        self.assertEqual(MENU_FUTURE, "두 미래 출하일에 감귤을 팔 때를 비교해 주세요.")

    def test_current_shows_gross_amount_and_asks_for_period(self):
        result = {"status": "NEEDS_INPUT", "input": {"quantity_kg": 3000, "current_price_per_kg": 3830, "price_basis": "gross", "options": []},
                  "missing_fields": ["cost_policy", "included_costs", "options", "current_fee_percent"]}
        text = current_comparison_guidance(result)
        self.assertIn("11,490,000원", text)
        self.assertIn("차감 전", text)
        self.assertIn("며칠 더 보관", text)
        self.assertNotIn("그 단가에서 이미 차감한 비용", text)
        self.assertNotIn("이후 확인할 조건", text)

    def test_current_no_unearned_net_proceeds(self):
        result = {"status": "NEEDS_INPUT", "input": {"quantity_kg": 3000, "current_price_per_kg": 3830, "price_basis": None},
                  "missing_fields": ["price_basis", "current_fee_percent"]}
        text = current_comparison_guidance(result)
        self.assertNotIn("11,490,000원", text)
        self.assertIn("수수료를 빼기 전", text)

    def test_current_no_silent_zero_assumptions(self):
        result = {"status": "NEEDS_INPUT", "input": {"quantity_kg": 3000, "current_price_per_kg": 3830,
                  "price_basis": "commission_net", "options": [{"id": "d7", "delay_days": 7}]},
                  "missing_fields": ["options.d7.loss_percent", "options.d7.daily_storage_cost"]}
        text = current_comparison_guidance(result)
        self.assertIn("7일 보관 대안", text)
        self.assertIn("감모율", text)
        self.assertNotIn("하루 보관비는 얼마", text)
        self.assertIn("0원으로 처리하지", text)

    def test_future_initial_short_dates(self):
        result = {"status": "NEEDS_INPUT", "comparison_inputs_pending": True,
                  "missing_fields": ["비교 물량(kg)", "보관 시작일", "A안 가정 단가(원/kg)",
                                     "B안 가정 단가(원/kg)", "A안 보관일", "B안 보관일", "A안 기간 전체 감모율(%)"]}
        text = future_pair_guidance(result)
        self.assertIn("어느 날짜와 어느 날짜", text)
        self.assertNotIn("비교 물량(kg)", text)
        self.assertNotIn("이후 확인할 조건", text)

    def test_future_known_inputs_no_pretend_profit(self):
        result = {"status": "NEEDS_INPUT", "missing_fields": ["A안 기간 전체 감모율(%)", "B안 기간 전체 감모율(%)"]}
        inputs = {"quantity": 3000, "price_a": 3830, "price_basis": "gross", "option_a": {"delay_days": 3}, "option_b": {"delay_days": 10}}
        text = future_pair_guidance(result, inputs)
        self.assertIn("11,490,000원", text)
        self.assertIn("출하 후 비교금액은 아닙니다", text)
        self.assertIn("3일", text)
        self.assertIn("10일 보관", text)

    def test_future_special_price_clarification_untouched(self):
        for status in ("NEEDS_CLARIFICATION", "BOX_PRICE_CONFIRMATION"):
            result = {"status": "NEEDS_INPUT", "source_status": status, "missing_fields": ["A안 가정 단가(원/kg)"]}
            self.assertIsNone(future_pair_guidance(result))

    def test_future_complete_is_not_missing(self):
        self.assertIsNone(future_pair_guidance({"status": "OK", "missing_fields": []}))

    def test_future_ignores_invalid_price_values(self):
        result = {"status": "NEEDS_INPUT", "missing_fields": ["B안 가정 단가(원/kg)"]}
        text = future_pair_guidance(result, {"quantity": "NaN", "price_a": "Infinity", "price_basis": "gross"})
        self.assertNotIn("nan", text.lower())
        self.assertIn("나중 출하할 때", text)

    def test_date_edit_renders_actual_days_without_pair_inputs(self):
        # Streamlit date-edit action passes only a pending result to render_dialogue.
        result = {"status": "NEEDS_INPUT", "missing_fields": ["B안 기간 전체 감모율(%)"],
                  "storage_days": {"A안": 3, "B안": 17},
                  "history_preview": {"clean_pairs": 15}}
        text = future_pair_guidance(result)
        self.assertIn("17일 보관", text)
        self.assertIn("과거 가격쌍 15개", text)
        self.assertNotIn("?일", text)

    def test_unknown_period_never_prints_question_mark_as_days(self):
        result = {"status": "NEEDS_INPUT", "missing_fields": ["B안 기간 전체 감모율(%)"]}
        text = future_pair_guidance(result)
        self.assertIn("해당 기간 보관", text)
        self.assertNotIn("?일", text)

    def test_history_preview_show_only_valid_counts(self):
        result = {"status": "NEEDS_INPUT", "missing_fields": ["A안 가정 단가(원/kg)"],
                  "history_preview": {"clean_pairs": 15}}
        self.assertIn("과거 가격쌍 15개", future_pair_guidance(result))


class QuickstartTests(unittest.TestCase):
    def _session(self):
        class FakeContext:
            def __init__(self):
                self.records = []
            def record(self, *args, **kwargs):
                self.records.append(args)
            def snapshot(self):
                return {"goal": "test"}
        class FakeStore:
            version = 0
            scenario = {}
            def update(self, *args, **kwargs):
                self.version += 1
                self.scenario = {}
                return {"status": "OK"}
            def snapshot(self):
                return {"version": self.version}
        class FakePair:
            version = 0
            inputs = {"quantity": None, "price_a": None, "price_basis": None}
            reset_calls = 0
            def reset(self):
                self.reset_calls += 1
            def apply(self, action, version, patch):
                self.version += 1
                return {"status": "NEEDS_INPUT", "missing_fields": ["보관 시작일", "A안 보관일"]}
        return SimpleNamespace(messages=["old"], task_context=None, price_history_query="old",
                               price_history_revision="old", store=FakeStore(), future_pair=FakePair()), FakeContext

    def test_future_starter_uses_no_model(self):
        fake, context = self._session()
        ctx_module = ModuleType("agents.juneok.task_context")
        ctx_module.TaskContext = context
        with patch.dict("sys.modules", {"agents.juneok.task_context": ctx_module}):
            reply = quickstart_reply(fake, MENU_FUTURE, lambda *_a, **_k: "날짜를 알려주세요", lambda *_: "보고서")
        self.assertEqual(reply["metrics"]["model_requests"], 0)
        self.assertEqual(reply["text"], "날짜를 알려주세요")
        self.assertEqual(fake.future_pair.version, 1)
        self.assertEqual(fake.messages, [])
        self.assertEqual(fake.task_context.records[0][0], "compare_future_pair")

    def test_current_starter_resets_only_explicit_new_comparison(self):
        fake, context = self._session()
        ctx_module = ModuleType("agents.juneok.task_context")
        ctx_module.TaskContext = context
        calc_module = ModuleType("agents.common.calculation_service")
        calc_module.compare_shipment_options = lambda _x: {"status": "NEEDS_INPUT", "missing_fields": ["quantity_kg"]}
        with patch.dict("sys.modules", {"agents.juneok.task_context": ctx_module,
                                      "agents.common.calculation_service": calc_module}):
            self.assertIsNone(quickstart_reply(fake, "감귤 물량은 3톤", lambda *_a, **_k: "", lambda *_: ""))
            reply = quickstart_reply(fake, MENU_NOW, lambda *_a, **_k: "물량을 알려주세요", lambda *_: "보고서")
        self.assertEqual(reply["metrics"]["model_requests"], 0)
        self.assertEqual(fake.store.version, 1)
        self.assertEqual(fake.future_pair.reset_calls, 1)
        self.assertEqual(fake.task_context.records[0][0], "compare_shipment_options")


if __name__ == "__main__":
    unittest.main()
