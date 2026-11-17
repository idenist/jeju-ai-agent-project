"""Direct-entry draft tests use the real Streamlit view without a model call."""
from decimal import Decimal
from unittest import TestCase

from streamlit.testing.v1 import AppTest


class FuturePairCostDraftTests(TestCase):
    def test_shared_cost_and_commission_toggles_preserve_each_draft(self):
        app = AppTest.from_string("""
import streamlit as st
from agents.future_pair_state import FuturePairState
from ui.future_pair_ui import render_future_pair

if "pair" not in st.session_state:
    pair = FuturePairState()
    cost = {"daily_storage_cost": 10000.0, "other_cost_krw": 5000.0, "fee_percent": 5.0}
    pair.apply("new", pair.version, {
        "quantity": 3000.0, "start_date": "2026-11-15", "price_basis": "gross", "price_a": 2500.0,
        "price_mode": "manual", "price_b": 2750.0,
        "option_a": {**cost, "delay_days": 3, "loss_percent": 1.0},
        "option_b": {"delay_days": 8, "loss_percent": 4.0, "daily_storage_cost": 20000.0,
                     "other_cost_krw": 6000.0, "fee_percent": 7.0},
    })
    st.session_state.pair = pair
if st.button("새 비교"):
    st.session_state.pair.reset()
render_future_pair(st.session_state.pair)
""").run()
        pair = app.session_state.pair

        def basis(value):
            app.selectbox(key="future_pair_basis").set_value(value).run()
            self.assertFalse(app.exception)

        def shared(enabled):
            app.checkbox(key="future_pair_shared_costs").set_value(enabled).run()
            self.assertFalse(app.exception)

        self.assertEqual(pair.inputs["option_b"]["fee_percent"], 7)
        shared(True)
        self.assertEqual(pair.inputs["option_b"]["fee_percent"], 5)
        shared(False)
        self.assertEqual(app.number_input(key="future_pair_B_fee").value, 7)

        basis("수수료 차감 후")
        self.assertFalse(any(field.key in {"future_pair_A_fee", "future_pair_B_fee"} for field in app.number_input))
        self.assertIsNone(pair.inputs["option_a"]["fee_percent"])
        self.assertIsNone(pair.inputs["option_b"]["fee_percent"])
        app.button(key="future_pair_calculate").click().run()
        self.assertEqual(Decimal(pair.current_result()["options_b"][0]["difference_vs_a_krw"]), 364000)
        basis("수수료 차감 전")
        self.assertIsNone(pair.current_result())
        self.assertEqual(app.number_input(key="future_pair_A_fee").value, 5)
        self.assertEqual(app.number_input(key="future_pair_B_fee").value, 7)

        shared(True)
        basis("수수료 차감 후")
        shared(False)
        self.assertEqual(app.number_input(key="future_pair_B_storage").value, 20000)
        self.assertEqual(app.number_input(key="future_pair_B_other").value, 6000)
        basis("수수료 차감 전")
        self.assertEqual(app.number_input(key="future_pair_A_fee").value, 5)
        self.assertEqual(app.number_input(key="future_pair_B_fee").value, 7)
        self.assertEqual(app.number_input(key="future_pair_price_a").value, 2500)
        self.assertEqual(app.number_input(key="future_pair_price_b").value, 2750)
        app.button(key="future_pair_calculate").click().run()
        self.assertEqual(Decimal(pair.current_result()["options_b"][0]["difference_vs_a_krw"]), 180850)

        next(button for button in app.button if button.label == "새 비교").click().run()
        basis("수수료 차감 전")
        self.assertIsNone(app.number_input(key="future_pair_A_fee").value)
        self.assertIsNone(app.number_input(key="future_pair_B_fee").value)
        self.assertIsNone(pair.current_result())
