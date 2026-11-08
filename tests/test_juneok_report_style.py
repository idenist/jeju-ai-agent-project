"""Report layout contract: canonical values, traceability, and end notes once."""
from copy import deepcopy
from io import BytesIO
import unittest

from pypdf import PdfReader
from agents.juneok.comparison import compare_shipment_options
from agents.juneok.reporting import render_report, REPORT_FOOTER, REPORT_NOTICES_HEADING
from agents.juneok.pdf_report import render_pdf_report
from agents.juneok.state import demo_scenario


class ReportStyleTests(unittest.TestCase):
    def entries(self):
        result = compare_shipment_options(demo_scenario())
        result["ui_context"] = {"version": 2, "input_context": "가상 예시·사용자 가정"}
        return [{"tool": "compare_shipment_options", "result": result}]

    def test_results_precede_consolidated_notices_and_no_developer_intro(self):
        entries = self.entries()
        before = deepcopy(entries)
        report = render_report(entries)
        body, notes = report.split("## " + REPORT_NOTICES_HEADING)
        self.assertNotIn("Python 도구의 조회·계산 결과입니다", report)
        self.assertNotIn("확인사항:", body)
        self.assertIn("3,109원/kg", body)
        self.assertIn("조건 버전 2", body)
        self.assertIn("표 1.", body)
        self.assertIn("물량", body)
        self.assertIn("9,000,000원", body)
        self.assertIn("전체 순이익이 아닙니다", notes)
        self.assertIn("보관비 외 기타 고정비용", notes)
        self.assertEqual(report.count(REPORT_FOOTER), 1)
        self.assertTrue(report.rstrip().endswith(REPORT_FOOTER))
        self.assertEqual(entries, before)

    def test_document_id_is_stable_and_changes_with_result(self):
        entries = self.entries()
        first = render_report(entries).splitlines()[2]
        self.assertEqual(first, render_report(deepcopy(entries)).splitlines()[2])
        scenario = demo_scenario()
        scenario["options"][0]["loss_percent"] = 4
        changed = [{"tool": "compare_shipment_options", "result": compare_shipment_options(scenario)}]
        self.assertNotEqual(first, render_report(changed).splitlines()[2])

    def test_pdf_has_one_scope_footer_per_page_and_preserves_notes(self):
        reader = PdfReader(BytesIO(render_pdf_report(self.entries())))
        text = "\n".join(page.extract_text() for page in reader.pages)
        self.assertNotIn("Python 도구의 조회·계산 결과입니다", text)
        self.assertEqual(text.count(REPORT_FOOTER), len(reader.pages))
        self.assertIn(REPORT_NOTICES_HEADING, text)
        self.assertIn("문서 ID: CIT-", text)
        self.assertIn("3,109원/kg", text)

    def test_shared_notices_deduplicated_without_losing_both_result_tables(self):
        entries = self.entries() * 2
        report = render_report(entries)
        body, notes = report.split("## " + REPORT_NOTICES_HEADING)
        self.assertEqual(body.count("3,109원/kg"), 2)
        self.assertEqual(notes.count("전체 순이익이 아닙니다"), 1)
        self.assertEqual(notes.count(REPORT_FOOTER), 1)


if __name__ == "__main__":
    unittest.main()
