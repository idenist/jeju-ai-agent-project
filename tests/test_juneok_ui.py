"""UI acceptance checks use no model calls and no API credentials."""
from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from agents.juneok.reporting import render_report, render_result
from agents.juneok.comparison import compare_shipment_options
from agents.juneok.state import demo_scenario
from agents.juneok.agent import AgentSession
from agents.juneok.config import Settings
from test_juneok_harness import FakeClient, call, response

ROOT = Path(__file__).resolve().parents[1]


class ReportTests(unittest.TestCase):
    def test_report_preserves_verified_example_and_unknown_feasibility(self):
        result = compare_shipment_options(demo_scenario())
        rendered = render_result(result)
        self.assertIn("3,109원/kg", rendered)
        self.assertIn("9,000,000원", rendered)
        self.assertIn("실행 가능성 미확인", rendered)
        self.assertIn("추천 순위를 제시하지 않습니다", rendered)
        report = render_report([{"tool": "compare_shipment_options", "result": result}])
        for value in ["3,109원/kg", "9,000,000원", "실행 가능성 미확인"]:
            self.assertIn(value, report)

    def test_missing_inputs_are_named_not_zero_filled(self):
        text = render_result(compare_shipment_options({}))
        self.assertIn("추가 입력", text)
        self.assertIn("물량(kg)", text)
        self.assertNotIn("0원", text)


class UiTests(unittest.TestCase):
    def test_real_shared_session_chat_card_chat_and_rerun(self):
        responses = [response(call("update_scenario", {"expected_version": 0, "patch": demo_scenario(), "finish_turn": True})),
                     response(call("compare_shipment_options", {"expected_version": 2, "finish_turn": True}))]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_A_REAL_KEY"}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
            app.session_state.workspace.session.client = FakeClient(responses)
            app.chat_input[0].set_value("가상 조건을 입력할게요").run()
            self.assertEqual(list(app.exception), [])
            self.assertEqual(app.session_state.quantity, "3000")
            self.assertEqual(app.session_state.loss_0, "2")
            self.assertEqual(app.session_state.card_option_ids[0], "seven_days")
            next(x for x in app.text_input if x.key == "loss_0").set_value("4").run()
            self.assertTrue(app.chat_input[0].disabled)
            self.assertEqual(len(app.get("download_button")), 0)
            self.assertTrue(any("카드에 저장 전 수정" in x.label for x in app.expander))
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
            self.assertFalse(app.chat_input[0].disabled)
            self.assertIs(app.session_state.workspace.store, app.session_state.agent_session.store)
            app.chat_input[0].set_value("방금 카드의 조건을 다시 확인해 줘").run()
            self.assertEqual(list(app.exception), [])
            self.assertEqual(app.session_state.workspace.store.version, 2)
            self.assertEqual(app.session_state.loss_0, "4")
            self.assertTrue(any("3,174원/kg" in str(x.value) for x in app.markdown))
            app.run()
            self.assertEqual(app.session_state.workspace.store.version, 2)

    def test_pdf_failure_preserves_preview_conditions_and_recovers(self):
        app = self.make_app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch("agents.juneok.pdf_report.render_pdf_report", side_effect=RuntimeError("TEST_FAILURE")):
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            self.assertEqual(list(app.exception), [])
            self.assertTrue(any("PDF를 만들지 못했습니다" in x.value for x in app.error))
            self.assertTrue(any("3,109원/kg" in str(x.value) for x in app.markdown))
            self.assertEqual(app.session_state.workspace.store.scenario["quantity_kg"], 3000)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            app.run()
            self.assertEqual(list(app.error), [])
            self.assertTrue(any(b.label == "PDF 보고서 내려받기" for b in app.get("download_button")))

    def test_csv_preview_apply_is_explicit_and_resets_only_chat(self):
        import io
        from test_juneok_csv_import import CsvImportTests
        from agents.juneok.csv_import import preview_auction_csv, apply_auction_preview
        from agents.juneok.data_refresh import market_status
        fixture = CsvImportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        upload = io.BytesIO(fixture.payload([fixture.new]))
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), \
                patch("streamlit.file_uploader", return_value=upload), \
                patch("agents.juneok.csv_import.preview_auction_csv", side_effect=lambda b: preview_auction_csv(b, fixture.root, fixture.today)), \
                patch("agents.juneok.csv_import.apply_auction_preview", side_effect=lambda p: apply_auction_preview(p, fixture.root, fixture.today)):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            next(b for b in app.button if b.label == "CSV 검사 · 변경 내용 미리보기").click().run()
            self.assertEqual(list(app.exception), [])
            self.assertEqual(market_status(fixture.root, fixture.today)["snapshot_id"], "shared-baseline")
            self.assertTrue(app.session_state.csv_preview)
            app.session_state.agent_messages = [{"role": "user", "content": "이전 자료 질문"}]
            next(b for b in app.button if b.label == "확인한 자료 적용 · 대화 초기화").click().run()
            self.assertEqual(list(app.exception), [])
            self.assertEqual(app.session_state.quantity, "3000")
            self.assertEqual(app.session_state.agent_messages, [])
            self.assertEqual(market_status(fixture.root, fixture.today)["observed_last_date"], "2026-11-04")

    def test_csv_failure_keeps_previous_data_and_clears_preview(self):
        import io
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), \
                patch("streamlit.file_uploader", return_value=io.BytesIO(b"bad,csv\n")):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
            next(b for b in app.button if b.label == "CSV 검사 · 변경 내용 미리보기").click().run()
            self.assertEqual(list(app.exception), [])
            self.assertTrue(any("명세와 다릅니다" in x.value for x in app.error))
            self.assertFalse(any(b.label == "확인한 자료 적용 · 대화 초기화" for b in app.button))

    def test_chat_success_label_report_and_rerun_preserve_results(self):
        session = AgentSession(Settings(model="mock"), FakeClient([response(call("update_scenario", {
            "expected_version": 0, "patch": demo_scenario(), "finish_turn": True}))]))
        reply = session.run("가상 조건")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_A_REAL_KEY"}), patch.object(AgentSession, "run", return_value=reply):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
            app.chat_input[0].set_value("조건 비교").run()
            self.assertEqual(list(app.exception), [])
            self.assertTrue(any("호출 성공" in str(x.value) for x in app.sidebar.markdown))
            self.assertTrue(any("3,109원/kg" in str(x.value) for x in app.markdown))
            self.assertEqual(app.session_state.agent_messages[-1]["reply"]["report"], reply["report"])
            app.run()
            self.assertEqual(list(app.exception), [])
            self.assertTrue(any("3,109원/kg" in str(x.value) for x in app.markdown))
            self.assertEqual({b.label for b in app.get("download_button")}, {"PDF 보고서 내려받기", "MD 내려받기", "비교 JSON 내려받기"})
            with patch.dict(os.environ, {"OPENAI_MODEL": "different-model"}):
                app.run()
                self.assertTrue(any("실제 호출 전" in str(x.value) for x in app.sidebar.markdown))

    def make_app(self):
        # Blank OS override makes tests independent of the developer's product .env.
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
        self.assertEqual(list(app.exception), [])
        return app

    def test_keyless_screen_has_offline_tools_and_disabled_chat(self):
        app = self.make_app()
        self.assertTrue(app.chat_input[0].disabled)
        self.assertTrue(any("API 키 미설정" in str(x.value) for x in app.sidebar.markdown))
        self.assertTrue(any(x.value == "현재 조건 카드" for x in app.subheader))

    def test_explicit_demo_can_be_calculated_without_key(self):
        app = self.make_app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertTrue(any("3,109원/kg" in str(x.value) for x in app.markdown))

    def test_sensitivity_requires_user_values_and_clears_changed_grid(self):
        app = self.make_app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
            next(b for b in app.button if b.label == "가정 범위 비교").click().run()
            self.assertEqual(app.session_state.sensitivity_result["status"], "INVALID_INPUT")
            next(x for x in app.text_input if x.key == "sensitivity_losses").set_value("2,4").run()
            next(x for x in app.text_input if x.key == "sensitivity_prices").set_value("3000,3300").run()
            next(b for b in app.button if b.label == "가정 범위 비교").click().run()
            self.assertEqual(app.session_state.sensitivity_result["status"], "OK")
            self.assertEqual(len(app.session_state.sensitivity_result["rows"]), 4)
            self.assertTrue(any("3,174원/kg" in str(x.value) for x in app.markdown))
            next(x for x in app.text_input if x.key == "sensitivity_losses").set_value("1").run()
            with self.assertRaises(KeyError):
                app.session_state["sensitivity_result"]
        self.assertEqual(list(app.exception), [])

    def test_empty_form_requests_information(self):
        app = self.make_app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertTrue(any("추가 입력이 필요" in str(x.value) for x in app.markdown))

    def test_edit_hides_previous_result_report_and_sensitivity_until_recalculated(self):
        app = self.make_app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
            self.assertEqual(app.session_state.workspace.store.version, 1)
            self.assertEqual({b.label for b in app.get("download_button")}, {"PDF 보고서 내려받기", "MD 내려받기", "비교 JSON 내려받기"})
            next(x for x in app.text_input if x.key == "loss_0").set_value("4").run()
            self.assertTrue(any("입력 조건이 바뀌었습니다" in x.value for x in app.warning))
            self.assertFalse(any("3,109원/kg" in str(x.value) for x in app.markdown))
            self.assertEqual(len(app.get("download_button")), 0)
            self.assertTrue(next(b for b in app.button if b.label == "가정 범위 비교").disabled)
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
            self.assertEqual(app.session_state.workspace.store.version, 2)
            self.assertEqual(app.session_state.quantity, "3000")
            self.assertEqual(app.session_state.price, "3000")
            self.assertTrue(any("3,174원/kg" in str(x.value) for x in app.markdown))
            self.assertTrue(any("대안 1 기간 전체 감모율" in str(x.value) for x in app.markdown))
            self.assertEqual(list(app.exception), [])

    def test_period_can_be_reconfirmed_before_next_submit(self):
        app = self.make_app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
            next(x for x in app.number_input if x.key == "days_0").set_value(3).run()
            self.assertFalse(next(x for x in app.checkbox if x.key == "period_0").value)
            next(x for x in app.checkbox if x.key == "period_0").check().run()
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
            self.assertEqual(app.session_state.workspace.current_entries()[0]["result"]["status"], "OK")
            self.assertEqual(app.session_state.workspace.store.scenario["options"][0]["delay_days"], 3)
            self.assertEqual(app.session_state.workspace.store.scenario["options"][0]["loss_percent"], 2)
            self.assertEqual(list(app.exception), [])

    def test_net_fee_contradiction_is_not_silently_ignored(self):
        app = self.make_app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            next(x for x in app.text_input if x.key == "current_fee").set_value("5")
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertTrue(any("다시 차감할 수 없습니다" in str(x.value) for x in app.error))

    def test_period_edit_requires_fresh_confirmation(self):
        app = self.make_app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
            next(x for x in app.number_input if x.key == "days_0").set_value(3)
            next(b for b in app.button if b.label == "입력 조건 비교").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertFalse(next(x for x in app.checkbox if x.key == "period_0").value)
        self.assertTrue(any("추가 입력이 필요" in str(x.value) for x in app.markdown))

    def test_real_historical_query_is_available_without_model(self):
        app = self.make_app()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            next(b for b in app.button if b.label == "과거 출하량 조회").click().run()
        self.assertEqual(list(app.exception), [])
        self.assertTrue(any("43781.08" in str(x.value) for x in app.markdown))
        self.assertTrue(any("2025-09-17" in str(x.value) for x in app.markdown))


if __name__ == "__main__":
    unittest.main()
