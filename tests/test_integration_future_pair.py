from decimal import Decimal
from io import BytesIO
import json
import os
import unittest
from unittest.mock import patch

from agents.common.future_pair import compare_future_pair
from agents.juneok.config import PROJECT_ROOT
from agents.juneok.pdf_report import render_pdf_report
from agents.juneok.presentation import select_results
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
        report = render_report([{"tool": "compare_future_pair", "result": result}])
        self.assertLess(report.index("A안 대비 차액"), report.index("기간 전체 감모"))
        self.assertIn("과거 동일 규격 경락가격쌍", report)
        self.assertNotIn("trade_type", report)

    def test_exact_whole_won_break_even_can_be_a_tie(self):
        option_a = {**OPTION_A, "loss_percent": 0, "daily_storage_cost": 0, "other_cost_krw": 0, "fee_percent": 0}
        result = compare_future_pair(
            **{**COMMON, "option_a": option_a},
            option_b={**option_a, "delay_days": 10}, price_b=2500,
        )
        self.assertEqual(Decimal(result["minimum_whole_krw_per_kg"]), 2500)
        self.assertEqual(Decimal(result["options_b"][0]["difference_vs_a_krw"]), 0)
        report = render_report([{"tool": "compare_future_pair", "result": result}])
        self.assertIn("A 이상을 위한 B안 최소 단가: 2,500원/kg", report)

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
        from pypdf import PdfReader
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
        first_a = ws.session.future_pair.current_result()["option_a"]
        self.assertIn("2,660원/kg", "\n".join(item.value for item in app.markdown))

        app.number_input(key="future_pair_B_days").set_value(8).run()
        self.assertIsNone(app.number_input(key="future_pair_B_loss_8").value)
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))
        app.number_input(key="future_pair_B_loss_8").set_value(4.0)
        app.button(key="future_pair_calculate").click().run()
        warning = next(item.value for item in app.warning if "5일" in item.value)
        self.assertEqual(warning.count("B안 단가를 직접 입력"), 1)
        pair = ws.session.future_pair
        snapshot = pair.snapshot()
        conflict = pair.apply("edit", pair.version, {"price_mode": "history", "price_b": 2750})
        self.assertEqual(conflict["status"], "INVALID_INPUT")
        self.assertEqual(pair.snapshot(), snapshot)

        app.radio(key="future_pair_price_mode").set_value("직접 입력").run()
        app.number_input(key="future_pair_price_b").set_value(2750.0)
        with patch("streamlit.download_button", wraps=st.download_button) as download:
            app.button(key="future_pair_calculate").click().run()
        self.assertFalse(app.exception)
        outputs = {call.args[0]: call.args[1] for call in download.call_args_list}
        result = json.loads(outputs["미래 A/B 결과 JSON 내려받기"])["result"]
        self.assertEqual(result["option_a"], first_a)
        self.assertEqual(Decimal(result["options_b"][0]["difference_vs_a_krw"]), 261850)
        pdf = PdfReader(BytesIO(outputs["미래 A/B PDF 내려받기"]))
        pdf_text = "\n".join(page.extract_text() for page in pdf.pages)
        screen = "\n".join(item.value for item in app.markdown)
        for value in ("7,018,750원", "7,280,600원", "261,850원", "A 이상을 위한 B안 최소 단가"):
            self.assertIn(value, screen)
            self.assertIn(value, pdf_text)
        self.assertEqual(app.session_state.workspace.store.snapshot(), original_scenario)

    def test_chat_conditions_project_to_card_and_only_current_result_is_exported(self):
        import streamlit as st
        from pypdf import PdfReader
        from streamlit.testing.v1 import AppTest
        from test_integration_ui_journey import 모의_대화_클라이언트

        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
        ws = app.session_state.workspace
        ws.load_demo()
        before_entries = ws.current_entries()
        self.assertTrue(before_entries)
        original = ws.store.snapshot()
        state = ws.session.future_pair
        client = 모의_대화_클라이언트("compare_future_pair", {})
        ws.session.client = client

        def say(action, changes, version=None):
            client.arguments = {"action": action, "patch": changes,
                                "expected_version": state.version if version is None else version}
            return ws.run("두 미래 출하안을 비교합니다.")["visible_results"][0]["result"]

        def rerun():
            with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
                app.run()

        missing = say("new", {**COMMON, "option_b": {**OPTION_B, "delay_days": 8}})
        self.assertEqual(missing["status"], "NEEDS_INPUT")
        self.assertIn("B안 가정 단가(원/kg)", missing["missing_fields"])
        first = say("edit", {"price_b": 2750})
        self.assertEqual(first["status"], "OK")
        self.assertEqual(Decimal(first["options_b"][0]["difference_vs_a_krw"]), 261850)
        revision = state.version
        self.assertEqual(say("edit", {"option_b": {"delay_days": 1}})["status"], "INVALID_INPUT")
        self.assertEqual(state.version, revision)
        self.assertEqual(say("edit", {"price_basis": "commission_net"})["status"], "INVALID_INPUT")
        self.assertEqual(say("edit", {"option_b": {"fee_percent": 99.999}})["status"], "INVALID_INPUT")
        rerun()
        self.assertFalse(app.exception)
        self.assertEqual(app.number_input(key="future_pair_B_days").value, 8)
        self.assertEqual(app.number_input(key="future_pair_price_b").value, 2750)
        changed = say("edit", {"option_b": {"delay_days": 9, "loss_percent": 4}})
        self.assertEqual(changed["status"], "NEEDS_INPUT")
        self.assertIn("B안 기간 전체 감모율(%)", changed["missing_fields"])
        entries = [{"tool": "compare_future_pair", "result": result} for result in (first, changed)]
        self.assertEqual(select_results(entries, ws.store.version), entries[1:])
        self.assertEqual(say("edit", {"price_b": 1}, version=revision)["status"], "INVALID_INPUT")
        rerun()
        self.assertFalse(app.exception)
        self.assertEqual(app.number_input(key="future_pair_B_days").value, 9)
        self.assertIsNone(app.number_input(key="future_pair_B_loss_9").value)
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))

        final = say("edit", {"option_b": {"loss_percent": 4}})
        self.assertEqual(final["option_a"], first["option_a"])
        self.assertEqual(Decimal(final["options_b"][0]["difference_vs_a_krw"]), 251850)
        with patch("streamlit.download_button", wraps=st.download_button) as download:
            rerun()
        self.assertFalse(app.exception)
        outputs = {call.args[0]: call.args[1] for call in download.call_args_list}
        result = json.loads(outputs["미래 A/B 결과 JSON 내려받기"])["result"]
        self.assertEqual(result, state.current_result())
        self.assertIn("251,850원", "\n".join(item.value for item in app.markdown))
        pdf = PdfReader(BytesIO(outputs["미래 A/B PDF 내려받기"]))
        self.assertIn("251,850원", "\n".join(page.extract_text() for page in pdf.pages))
        self.assertEqual(ws.store.snapshot(), original)
        self.assertEqual(ws.current_entries(), before_entries)

        client.arguments = {"action": "edit", "patch": {"price_b": 2800}, "expected_version": state.version}
        requests_before = len(client.requests)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            self.assertFalse(app.chat_input[0].disabled)
            app.chat_input[0].set_value("B안 단가를 2,800원으로 바꿔줘").run()
        self.assertFalse(app.exception)
        self.assertEqual(len(client.requests), requests_before + 1)
        self.assertEqual(state.current_result()["options_b"][0]["price_per_kg"], "2800")
        self.assertEqual(app.number_input(key="future_pair_price_b").value, 2800)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            app.number_input(key="future_pair_price_b").set_value(2750).run()
        self.assertIsNone(state.current_result())
        self.assertEqual(state.inputs["price_b"], 2750)
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))

    def test_chat_history_recalculates_exact_gap_and_recovers_with_manual_price(self):
        import streamlit as st
        from pypdf import PdfReader
        from streamlit.testing.v1 import AppTest
        from test_integration_ui_journey import 모의_대화_클라이언트

        app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=30).run()
        ws = app.session_state.workspace
        pair = ws.session.future_pair
        client = 모의_대화_클라이언트("compare_future_pair", {})
        ws.session.client = client

        def compare(action, changes):
            client.arguments = {"action": action, "patch": changes, "expected_version": pair.version}
            reply = ws.run("두 미래 출하안의 손익을 비교합니다.")
            self.assertEqual(reply["metrics"]["model_requests"], 1)
            return reply["visible_results"][0]["result"]

        first = compare("new", {**COMMON, "option_b": OPTION_B, "price_mode": "history",
                                "history_filters": {**FILTERS, "grade": None}})
        self.assertEqual(first["status"], "NEEDS_INPUT")
        self.assertEqual(first["source_status"], "NEEDS_CLARIFICATION")
        self.assertIn("특", first["choices"]["grade"])
        self.assertIsNone(pair.current_result())
        seven = compare("edit", {"history_filters": {"grade": "특"}})
        self.assertEqual(seven["interval_days"], 7)
        self.assertEqual(len(seven["options_b"]), 3)
        self.assertEqual(seven["evidence"][seven["options_b"][0]["id"]]["clean_pairs"], 20)

        fourteen = compare("edit", {"option_b": {"delay_days": 17, "loss_percent": 4, "loss_confirmed_for_period": True}})
        self.assertEqual(fourteen["option_a"], seven["option_a"])
        self.assertEqual(fourteen["interval_days"], 14)
        self.assertEqual(pair.inputs["history_filters"], FILTERS)
        self.assertIsNone(pair.inputs["price_b"])
        with patch("streamlit.download_button", wraps=st.download_button) as download:
            app.run()
        self.assertFalse(app.exception)
        files = {call.args[0]: call.args[1] for call in download.call_args_list}
        saved = json.loads(files["미래 A/B 결과 JSON 내려받기"])["result"]
        self.assertEqual(saved, pair.current_result())
        source = saved["evidence"][saved["options_b"][0]["id"]]
        self.assertEqual(source["clean_pairs"], 15)
        self.assertTrue(source["source"]["comparison_sha256"])
        pdf = PdfReader(BytesIO(files["미래 A/B PDF 내려받기"]))
        pdf_text = "\n".join(page.extract_text() for page in pdf.pages)
        screen = "\n".join(item.value for item in app.markdown)
        for value in ("7,018,750원", "15쌍", "과거 동일 규격 경락가격쌍", *source["base_date_range"]):
            self.assertIn(value, pdf_text)
            self.assertIn(value, screen)

        with patch("agents.juneok.future_pair_state._price_revision", return_value=("changed",)):
            self.assertIsNone(pair.current_result())
        unsupported = compare("edit", {"option_b": {"delay_days": 8, "loss_percent": 4, "loss_confirmed_for_period": True}})
        self.assertEqual(unsupported["status"], "NO_DATA")
        self.assertEqual(unsupported["source_status"], "INTERVAL_NOT_AVAILABLE")
        self.assertEqual(unsupported["message"].count("B안 단가를 직접 입력"), 1)
        app.run()
        self.assertFalse(any(button.label.startswith("미래 A/B") for button in app.get("download_button")))
        with patch("agents.common.future_pair.verify_integrated_price_sources", return_value={"status": "DATA_ERROR"}):
            error = compare("edit", {"option_b": {"delay_days": 10, "loss_percent": 4, "loss_confirmed_for_period": True}})
        self.assertEqual(error["status"], "DATA_ERROR")
        self.assertTrue(error["manual_price_required"])
        self.assertIsNone(pair.current_result())
        self.assertEqual(pair.inputs["history_filters"], FILTERS)
        with patch("agents.common.future_pair.verify_integrated_price_sources", side_effect=AssertionError):
            manual = compare("edit", {"price_mode": "manual", "price_b": 2750,
                                      "option_b": {"delay_days": 8, "loss_percent": 4, "loss_confirmed_for_period": True}})
        self.assertEqual(manual["option_a"], seven["option_a"])
        self.assertEqual(Decimal(manual["options_b"][0]["difference_vs_a_krw"]), 261850)
        self.assertIsNone(pair.inputs["history_filters"])
