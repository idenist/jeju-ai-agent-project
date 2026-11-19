"""Regression coverage for the active comparison's market specification."""
import unittest
from unittest.mock import patch

from services.market_decision import market_spec_identity
from ui.seasonal_threshold_ui import render_threshold_evidence

OLD_SPEC = {"market_code": "110001", "variety": "조생귤", "grade": "특",
            "size": "소", "package": "상자", "package_kg": "5", "trade_type": "경매"}
NEW_SPEC = {**OLD_SPEC, "variety": "하우스감귤"}


def _pair(spec, version=2):
    return {"status": "OK", "analysis_type": "future_pair", "price_basis": "gross",
            "future_pair_version": version, "options_b": [{"id": "mid"}],
            "evidence": {"mid": {"conditions": spec}} if spec else {}}


class SeasonalThresholdUiTests(unittest.TestCase):
    def test_changed_product_uses_current_spec_and_resets_old_draft(self):
        with (
            patch("ui.seasonal_threshold_ui.st") as st,
            patch("ui.seasonal_threshold_ui._spec_form", return_value=(None, False)),
            patch("ui.seasonal_threshold_ui._display"),
            patch("ui.seasonal_threshold_ui.evaluate_market_decision", return_value={"status": "OK"}) as evaluate,
        ):
            st.session_state = {
                "seasonal_prices_result": {"status": "OK", "conditions": {**OLD_SPEC, "window_days": 3}},
                "market_decision_custom_spec": {**OLD_SPEC, "window_days": 14},
                "market_decision_custom_scope": market_spec_identity(OLD_SPEC),
                "market_decision_spec_chat_window": 14,
            }
            pair = _pair(NEW_SPEC)
            render_threshold_evidence(pair, "chat")
            evaluate.assert_called_once_with(pair, {**NEW_SPEC, "window_days": 7})
            self.assertNotIn("market_decision_spec_chat_window", st.session_state)

    def test_same_product_preserves_confirmed_window_after_recalculation(self):
        with (
            patch("ui.seasonal_threshold_ui.st") as st,
            patch("ui.seasonal_threshold_ui._spec_form", return_value=(None, False)),
            patch("ui.seasonal_threshold_ui._display"),
            patch("ui.seasonal_threshold_ui.evaluate_market_decision", return_value={"status": "OK"}) as evaluate,
        ):
            st.session_state = {
                "market_decision_custom_spec": {**NEW_SPEC, "window_days": 3},
                "market_decision_custom_scope": market_spec_identity(NEW_SPEC),
            }
            spec = {**NEW_SPEC, "package_kg": "5.00"}
            pair = _pair(spec, version=3)
            render_threshold_evidence(pair, "chat")
            evaluate.assert_called_once_with(pair, {**spec, "window_days": 3})

    def test_legacy_pair_requires_confirmation_and_discards_it_on_pair_change(self):
        selection = {**OLD_SPEC, "window_days": 3}
        with (
            patch("ui.seasonal_threshold_ui.st") as st,
            patch("ui.seasonal_threshold_ui._spec_form", side_effect=[
                (None, False), (selection, False), (None, False)]) as form,
            patch("ui.seasonal_threshold_ui._display"),
            patch("ui.seasonal_threshold_ui.evaluate_market_decision", return_value={"status": "OK"}) as evaluate,
        ):
            st.session_state = {
                "seasonal_prices_result": {"status": "OK", "conditions": selection},
                "market_decision_latest_chat": {"result": {"status": "OK"}},
            }
            pair = _pair(None)
            render_threshold_evidence(pair, "chat")
            form.assert_called_once_with("chat", selection, None)
            evaluate.assert_not_called()
            self.assertNotIn("market_decision_latest_chat", st.session_state)
            render_threshold_evidence(pair, "chat")
            evaluate.assert_called_once_with(pair, selection)
            render_threshold_evidence(_pair(None, version=3), "chat")
            self.assertEqual(evaluate.call_count, 1)
            self.assertNotIn("market_decision_latest_chat", st.session_state)
