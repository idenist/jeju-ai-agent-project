"""Direct-entry draft tests use the real Streamlit view without a model call."""
from decimal import Decimal
from datetime import date
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

        app.radio(key="future_pair_loss_mode").set_value("판매 가능량(kg)").run()
        self.assertEqual(app.number_input(key="future_pair_A_sale_weight_3").value, 2970)
        self.assertEqual(app.number_input(key="future_pair_B_sale_weight_8").value, 2880)
        app.number_input(key="future_pair_B_sale_weight_8").set_value(2850).run()
        self.assertEqual(pair.inputs["option_b"]["loss_percent"], 5)
        self.assertEqual(pair.inputs["option_a"]["loss_percent"], 1)
        app.number_input(key="future_pair_B_sale_weight_8").set_value(3010).run()
        self.assertIsNone(pair.inputs["option_b"]["loss_percent"])
        self.assertIsNone(pair.current_result())
        app.number_input(key="future_pair_B_sale_weight_8").set_value(2880).run()
        app.radio(key="future_pair_loss_mode").set_value("감모율(%)").run()
        self.assertEqual(app.number_input(key="future_pair_B_loss_8").value, 4)

        app.radio(key="future_pair_price_mode").set_value("손익분기만 보기").run()
        self.assertFalse(any(field.key == "future_pair_price_b" for field in app.number_input))
        app.button(key="future_pair_calculate").click().run()
        self.assertTrue(pair.current_result()["threshold_only"])
        app.radio(key="future_pair_price_mode").set_value("직접 입력").run()
        self.assertEqual(app.number_input(key="future_pair_price_b").value, 2750)

        app.selectbox(key="future_pair_quantity_unit").select("톤").run()
        self.assertEqual(app.number_input(key="future_pair_quantity_tons").value, 3)
        app.number_input(key="future_pair_quantity_tons").set_value(3.2).run()
        app.selectbox(key="future_pair_quantity_unit").select("kg").run()
        self.assertEqual(app.number_input(key="future_pair_quantity").value, 3200)
        app.number_input(key="future_pair_quantity").set_value(3000).run()

        app.radio(key="future_pair_schedule_mode").set_value("출하일로 입력").run()
        self.assertEqual(app.date_input(key="future_pair_A_sale_date").value, date(2026, 11, 18))
        app.date_input(key="future_pair_B_sale_date").set_value(date(2026, 11, 25)).run()
        self.assertEqual(pair.inputs["option_b"]["delay_days"], 10)
        self.assertEqual(pair.inputs["option_a"]["loss_percent"], 1)
        self.assertIsNone(pair.inputs["option_b"]["loss_percent"])
        app.radio(key="future_pair_schedule_mode").set_value("보관일로 입력").run()
        self.assertEqual(app.number_input(key="future_pair_B_days").value, 10)

        app.selectbox(key="future_pair_price_a_unit").select("원/상자").run()
        app.number_input(key="future_pair_price_a_box_amount").set_value(12500).run()
        self.assertIsNone(pair.inputs["price_a"])
        app.number_input(key="future_pair_price_a_box_weight").set_value(5.0).run()
        self.assertEqual(pair.inputs["price_a"], 2500)
        app.selectbox(key="future_pair_price_a_unit").select("원/kg").run()
        self.assertEqual(app.number_input(key="future_pair_price_a").value, 2500)
        app.selectbox(key="future_pair_price_a_unit").select("원/상자").run()
        self.assertEqual(app.number_input(key="future_pair_price_a_box_weight").value, 5)
        self.assertEqual(pair.inputs["price_b"], 2750)

        next(button for button in app.button if button.label == "새 비교").click().run()
        basis("수수료 차감 전")
        self.assertIsNone(app.number_input(key="future_pair_A_fee").value)
        self.assertIsNone(app.number_input(key="future_pair_B_fee").value)
        self.assertIsNone(pair.current_result())
