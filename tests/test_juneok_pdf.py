"""Offline PDF content/structure checks; layout is separately rendered for review."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from agents.juneok.comparison import compare_shipment_options
from agents.juneok.evidence import get_historical_shipment_context
from agents.juneok.pdf_report import render_pdf_report
from agents.juneok.sensitivity import compare_sensitivity
from agents.juneok.state import demo_scenario


def read_pdf(results):
    data = render_pdf_report(results)
    reader = PdfReader(BytesIO(data))
    return data, reader, "\n".join(page.extract_text() for page in reader.pages)


class PdfTests(unittest.TestCase):
    def test_handoff_reports_do_not_split_short_notice_box(self):
        from agents.juneok.agent import AgentSession
        from agents.juneok.config import Settings
        from agents.juneok.workspace import SharedWorkspace, card_values
        ws = SharedWorkspace(AgentSession(Settings(api_key="", model="offline-demo")))
        ws.load_demo()
        for grid in [False, True]:
            if grid:
                values, ids = card_values(ws.store.snapshot())
                values["loss_0"] = "4"
                ws.apply_card(values, ws.store.version, ids)
                ws.sensitivity(ids[0], [1, 2, 4], [3000, 3300])
            _, reader, _ = read_pdf(ws.current_entries())
            pages = [page.extract_text() for page in reader.pages]
            if not grid:
                self.assertEqual(len(pages), 1)
            notice_pages = [p for p in pages if "이용 안내 및 확인사항" in p]
            self.assertEqual(len(notice_pages), 1)
            self.assertIn("산출 해석", notice_pages[0])
            self.assertIn("원 단위 최소 단가는", notice_pages[0])

    def test_korean_numbers_input_context_and_actions_survive(self):
        result = compare_shipment_options(demo_scenario())
        result["ui_context"] = {"version": 2, "input_context": "가상 예시·사용자 가정", "changed_fields": ["기간 전체 감모율"]}
        original = deepcopy(result)
        with patch("socket.create_connection", side_effect=AssertionError("PDF export must be offline")):
            data, reader, text = read_pdf([{"tool": "compare_shipment_options", "result": result}])
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreaterEqual(len(reader.pages), 1)
        self.assertIn("제주 감귤 출하 비교 보고서", text)
        self.assertIn("3,109원/kg", text)
        self.assertIn("9,000,000원", text)
        self.assertIn("조건 버전 2", text)
        self.assertIn("가상 예시", text)
        self.assertIn("다음 확인과 행동", text)
        self.assertIn("실행 가능성 미확인", text)
        self.assertEqual(result, original)
        # A packaged font is actually embedded, rather than relying on the reader's OS.
        fonts = [font.get_object() for page in reader.pages for font in page["/Resources"]["/Font"].values()]
        embedded = [font for font in fonts if "/FontDescriptor" in font and "/FontFile2" in font["/FontDescriptor"]]
        self.assertTrue(embedded)

    def test_three_options_and_long_labels_are_not_dropped(self):
        scenario = demo_scenario()
        scenario["options"] = [{**scenario["options"][0], "id": "보관대안" + "가" * 30 + str(i),
                                "delay_days": 7 * i, "loss_percent": i * 2,
                                "future_price_per_kg": 3300} for i in [1, 2, 3]]
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "OK")
        _, _, text = read_pdf([{"tool": "compare_shipment_options", "result": result}])
        compact = re.sub(r"\s+", "", text)
        for option in result["options"]:
            self.assertIn(option["id"], compact)
            self.assertIn(f"{int(option['minimum_whole_krw_per_kg']):,}원/kg", compact)

    def test_all_49_sensitivity_rows_and_repeated_headers_survive(self):
        scenario = demo_scenario()
        result = compare_sensitivity(scenario, "seven_days", list(range(1, 8)), list(range(3000, 3700, 100)))
        self.assertEqual(len(result["rows"]), 49)
        _, reader, text = read_pdf([{"tool": "compare_sensitivity", "result": result}])
        self.assertGreater(len(reader.pages), 1)
        self.assertIn("2,970kg", reader.pages[0].extract_text())
        # One 'kg' cell per row, plus headers and fixed inputs; ensures no truncation at page breaks.
        self.assertEqual(len(re.findall(r"2,[789]\d\dkg", re.sub(r"\s+", "", text))), 49)
        for value in ["3,600", "7%", "3,109원/kg", "고정한 입력", "원 단위 최소 단가"]:
            self.assertIn(value.replace(" ", ""), re.sub(r"\s+", "", text))
        self.assertGreaterEqual(text.count("원 단위 최소 단가"), 2)

    def test_history_dates_source_and_absence_are_preserved(self):
        args = {"shipment_variety": "노지온주", "target_month": 11, "start_year": 2018, "end_year": 2024}
        found = get_historical_shipment_context(args)
        absent = get_historical_shipment_context({**args, "start_year": 2025, "end_year": 2025})
        self.assertEqual(found["status"], "OK")
        self.assertEqual(absent["status"], "NO_DATA")
        _, _, text = read_pdf([{"tool": "get_historical_shipment_context", "result": r} for r in [found, absent]])
        compact = re.sub(r"\s+", "", text)
        for value in ["#855", "2025-09-17", "https://www.jejudatahub.net/data/view/data/855", "조회 가능한 자료가 없습니다"]:
            self.assertIn(value.replace(" ", ""), compact)

    def test_markup_is_literal_and_unsupported_glyph_is_visible(self):
        scenario = demo_scenario()
        scenario["options"][0]["id"] = '<img src="https://example.com/x">🍊'
        result = compare_shipment_options(scenario)
        self.assertEqual(result["status"], "OK")
        _, _, text = read_pdf([{"tool": "compare_shipment_options", "result": result}])
        compact = re.sub(r"\s+", "", text)
        self.assertIn('<imgsrc="https://example.com/x">', compact)
        self.assertIn("[U+1F34A]", compact)

    def test_missing_packaged_font_fails_explicitly(self):
        from agents.juneok.pdf_report import _register_fonts
        _register_fonts.cache_clear()
        try:
            with patch("agents.juneok.pdf_report.FONT_DIR", Path("missing-font-fixture")):
                with self.assertRaisesRegex(ValueError, "글꼴"):
                    render_pdf_report([])
        finally:
            _register_fonts.cache_clear()


if __name__ == "__main__":
    unittest.main()
