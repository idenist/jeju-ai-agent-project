"""User-visible regressions from the navigation and market-selector revision."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
SPEC = {"market_code": "m1", "variety": "조생귤", "grade": "상",
        "size": "M", "package": "상자", "package_kg": "5", "trade_type": "경매"}


def catalog(*args):
    return {"status": "OK", "market_options": [{"code": "m1", "name": "첫 시장"},
                                                {"code": "m2", "name": "다른 시장"}],
            "available_filters": {key: [value] for key, value in SPEC.items()}}


class MarketSelectorTests(unittest.TestCase):
    def app(self, current):
        app = AppTest.from_string('''
import streamlit as st
from ui.market_inputs import render_market_spec_controls
selected, complete = render_market_spec_controls("example", st.session_state.current)
st.session_state.selected = selected
st.session_state.complete = complete
''')
        app.session_state.current = current
        return app.run()

    @patch("ui.market_inputs._market_catalog", side_effect=catalog)
    def test_decimal_weight_restores_existing_selection(self, _):
        app = self.app({**SPEC, "package_kg": 5.0})
        self.assertFalse(app.exception)
        self.assertTrue(app.session_state.complete)
        self.assertEqual(app.session_state.selected["package_kg"], "5")

    @patch("ui.market_inputs._market_catalog", side_effect=catalog)
    def test_market_change_requires_reselection_even_when_old_values_exist(self, _):
        app = self.app(SPEC)
        app.selectbox(key="example_market_code").select("m2").run()
        self.assertFalse(app.exception)
        self.assertFalse(app.session_state.complete)
        self.assertNotIn("variety", app.session_state.selected)
        self.assertIsNone(app.selectbox(key="example_variety").value)
        self.assertTrue(app.selectbox(key="example_grade").disabled)
        app.run()
        self.assertIsNone(app.selectbox(key="example_variety").value)

    @patch("ui.market_inputs._market_catalog", side_effect=catalog)
    def test_explicit_selection_from_another_view_updates_widgets(self, _):
        app = self.app(SPEC)
        app.session_state.current = {**SPEC, "market_code": "m2"}
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="example_market_code").value, "m2")
        self.assertTrue(app.session_state.complete)


class NavigationAndAdminTests(unittest.TestCase):
    def test_nested_tab_state_tracks_both_levels(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_A_REAL_KEY", "JEJU_ADMIN_PASSWORD": ""}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state.shipment_input_method, "AI와 대화")
            self.assertFalse(app.text_input(key="shipment_question").disabled)
            hidden_style = '[data-testid="stBottom"]:has'
            self.assertFalse(any(hidden_style in x.proto.body for x in app.get("html")))
            app.session_state.shipment_input_method = "직접 입력"
            app.run()
            self.assertTrue(any(hidden_style in x.proto.body for x in app.get("html")))
            app.session_state.shipment_input_method = "AI와 대화"
            app.session_state.shipment_view = "자료·도구"
            app.run()
            self.assertTrue(any(hidden_style in x.proto.body for x in app.get("html")))
            app.session_state.shipment_view = "출하 비교"
            app.run()
            self.assertFalse(any(hidden_style in x.proto.body for x in app.get("html")))

    def test_admin_gate_unicode_password_and_rotation(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "JEJU_ADMIN_PASSWORD": "관리자-테스트"}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
            self.assertFalse(any(b.label == "CSV 검사 · 변경 내용 미리보기" for b in app.button))
            next(x for x in app.text_input if x.label == "관리자 비밀번호").set_value("틀린 값")
            next(b for b in app.button if b.label == "관리자 확인").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(any(b.label == "CSV 검사 · 변경 내용 미리보기" for b in app.button))
            next(x for x in app.text_input if x.label == "관리자 비밀번호").set_value("관리자-테스트")
            next(b for b in app.button if b.label == "관리자 확인").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any(b.label == "CSV 검사 · 변경 내용 미리보기" for b in app.button))
            with patch.dict(os.environ, {"JEJU_ADMIN_PASSWORD": "변경한 값"}):
                app.run()
                self.assertFalse(any(b.label == "CSV 검사 · 변경 내용 미리보기" for b in app.button))

    def test_unconfigured_admin_has_no_upload_or_unlock(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "JEJU_ADMIN_PASSWORD": ""}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertFalse(any(b.label == "관리자 확인" for b in app.button))
            self.assertFalse(any(b.label == "CSV 검사 · 변경 내용 미리보기" for b in app.button))


class SeasonalViewTests(unittest.TestCase):
    def test_changed_source_hides_old_result(self):
        with patch("ui.seasonal_prices_ui.render_market_spec_controls", return_value=(SPEC, True)), \
             patch("ui.seasonal_prices_ui.seasonal_price_revision", return_value=["old"]) as stamp, \
             patch("ui.seasonal_prices_ui.compare_seasonal_prices", return_value={"status": "PARTIAL_DATA", "seasons": []}):
            app = AppTest.from_string("from ui.seasonal_prices_ui import render_seasonal_prices\nrender_seasonal_prices()").run()
            app.button(key="seasonal_prices_submit").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.session_state.seasonal_prices_result)
            stamp.return_value = ["changed"]
            app.run()
            self.assertFalse(app.exception)
            self.assertNotIn("seasonal_prices_result", app.session_state)
            self.assertTrue(any("자료가 바뀌어" in x.value for x in app.info))

    def test_data_error_is_explained_without_render_crash(self):
        with patch("ui.seasonal_prices_ui.render_market_spec_controls", return_value=(SPEC, True)), \
             patch("ui.seasonal_prices_ui.compare_seasonal_prices", return_value={"status": "DATA_ERROR", "message": "가격자료 읽기 오류"}):
            app = AppTest.from_string("from ui.seasonal_prices_ui import render_seasonal_prices\nrender_seasonal_prices()").run()
            app.button(key="seasonal_prices_submit").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("가격자료 읽기 오류" in x.value for x in app.warning))


if __name__ == "__main__":
    unittest.main()
