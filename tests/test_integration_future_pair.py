from decimal import Decimal
from io import BytesIO
import json
import unittest
from unittest.mock import patch

from agents.common.future_pair import compare_future_pair
from agents.juneok.config import PROJECT_ROOT
from agents.juneok.pdf_report import render_pdf_report
from agents.juneok.reporting import render_report


FILTERS = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}
OPTION_A = {"delay_days": 3, "loss_percent": 1, "daily_storage_cost": 10000, "other_cost_krw": 5000, "fee_percent": 5}
OPTION_B = {"delay_days": 10, "loss_percent": 4, "daily_storage_cost": 10000, "other_cost_krw": 5000, "fee_percent": 7}
COMMON = {"quantity": 3000, "price_basis": "gross", "start_date": "2026-11-12", "option_a": OPTION_A, "price_a": 2500}


class FuturePairTests(unittest.TestCase):
    def test_three_and_ten_day_comparison_uses_actual_history_and_net_amounts(self):
        result = compare_future_pair(**COMMON, option_b=OPTION_B, history_filters=FILTERS)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["interval_days"], 7)
        self.assertEqual(len(result["options_b"]), 3)
        a_net = Decimal(2970 * 2500) * Decimal(".95") - 35000
        self.assertEqual(Decimal(result["option_a"]["proceeds_krw"]), a_net)
        for row in result["options_b"]:
            self.assertEqual(row["delay_days"], 10)
            self.assertEqual(result["evidence"][row["id"]]["clean_pairs"], 20)
            b_net = Decimal(2880) * Decimal(row["price_per_kg"]) * Decimal(".93") - 105000
            self.assertEqual(Decimal(row["difference_vs_a_krw"]), b_net - a_net)
        threshold = (a_net + 105000) / (Decimal(2880) * Decimal(".93"))
        self.assertEqual(Decimal(result["break_even_vs_a_krw_per_kg"]), threshold)
        self.assertEqual(int(result["minimum_whole_krw_per_kg"]), 2660)

    def test_b_date_change_preserves_a_and_uses_new_pairs_and_costs(self):
        first = compare_future_pair(**COMMON, option_b=OPTION_B, history_filters=FILTERS)
        updated = compare_future_pair(**COMMON, option_b={**OPTION_B, "delay_days": 17}, history_filters=FILTERS)
        self.assertEqual(updated["status"], "OK")
        self.assertEqual(updated["option_a"], first["option_a"])
        self.assertEqual(updated["interval_days"], 14)
        self.assertEqual(updated["options_b"][0]["delay_days"], 17)
        self.assertEqual(Decimal(updated["options_b"][0]["total_cost_krw"]), 175000)
        self.assertEqual(updated["evidence"][updated["options_b"][0]["id"]]["clean_pairs"], 15)
        self.assertNotEqual(updated["minimum_whole_krw_per_kg"], first["minimum_whole_krw_per_kg"])

    def test_missing_interval_allows_manual_and_exports_same_amounts(self):
        from pypdf import PdfReader

        option_b = {**OPTION_B, "delay_days": 8}
        unavailable = compare_future_pair(**COMMON, option_b=option_b, history_filters=FILTERS)
        self.assertEqual(unavailable["status"], "INTERVAL_NOT_AVAILABLE")
        self.assertTrue(unavailable["manual_price_required"])
        with patch("agents.common.future_pair.verify_integrated_price_sources", side_effect=AssertionError):
            manual = compare_future_pair(**COMMON, option_b=option_b, price_b=2750)
        row = manual["options_b"][0]
        self.assertEqual(Decimal(row["proceeds_krw"]), 7280600)
        self.assertEqual(Decimal(row["difference_vs_a_krw"]), 261850)
        self.assertEqual(json.loads(json.dumps(manual)), manual)
        entries = [{"tool": "compare_future_pair", "result": manual}]
        pdf = PdfReader(BytesIO(render_pdf_report(entries)))
        text = "\n".join(page.extract_text() for page in pdf.pages)
        for value in ("7,280,600원", "261,850원"):
            self.assertIn(value, render_report(entries))
            self.assertIn(value, text)

    def test_screen_clears_old_result_and_switches_to_manual_price(self):
        import streamlit as st
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
        ws = app.session_state.workspace
        original_scenario = ws.store.snapshot()
        app.selectbox(key="future_pair_basis").set_value("수수료 차감 전").run()
        app.number_input(key="future_pair_quantity").set_value(3000.0)
        app.number_input(key="future_pair_price_a").set_value(2500.0)
        for side, option in (("A", OPTION_A), ("B", OPTION_B)):
            for suffix, field in [(f"loss_{option['delay_days']}", "loss_percent"), ("storage", "daily_storage_cost"),
                                  ("other", "other_cost_krw"), ("fee", "fee_percent")]:
                app.number_input(key=f"future_pair_{side}_{suffix}").set_value(float(option[field]))
        for key, value in FILTERS.items():
            app.text_input(key=f"future_pair_filter_{key}").set_value(value)
        app.button(key="future_pair_calculate").click().run()
        self.assertFalse(app.exception)
        first_a = app.session_state.future_pair_result[1]["option_a"]
        self.assertIn("2,660원/kg", "\n".join(item.value for item in app.markdown))

        app.number_input(key="future_pair_B_days").set_value(8).run()
        self.assertIsNone(app.number_input(key="future_pair_B_loss_8").value)
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))
        app.number_input(key="future_pair_B_loss_8").set_value(4.0)
        app.button(key="future_pair_calculate").click().run()
        warning = next(item.value for item in app.warning if "5일" in item.value)
        self.assertEqual(warning.count("B안 단가를 직접 입력"), 1)

        app.radio(key="future_pair_price_mode").set_value("직접 입력").run()
        app.number_input(key="future_pair_price_b").set_value(2750.0)
        with patch("streamlit.download_button", wraps=st.download_button) as download:
            app.button(key="future_pair_calculate").click().run()
        self.assertFalse(app.exception)
        outputs = {call.args[0]: call.args[1] for call in download.call_args_list}
        result = json.loads(outputs["미래 A/B 결과 JSON 내려받기"])["result"]
        self.assertEqual(result["option_a"], first_a)
        self.assertEqual(Decimal(result["options_b"][0]["difference_vs_a_krw"]), 261850)
        self.assertTrue(outputs["미래 A/B PDF 내려받기"].startswith(b"%PDF"))
        self.assertEqual(app.session_state.workspace.store.snapshot(), original_scenario)
