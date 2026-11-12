from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from agents.juneok.agent import AgentSession
from agents.juneok.workspace import SharedWorkspace
from agents.juneok.config import Settings
from agents.juneok.casefiles import parse_case, _canonical
from agents.juneok.case_changes import compare_cases
from agents.juneok.tools import execute_tool
from test_juneok_harness import FakeClient, call, response


class CaseChangesTests(unittest.TestCase):
    def workspace(self):
        ws = SharedWorkspace(AgentSession()); ws.load_demo()
        return ws

    def test_loss_change_recalculates_both_without_restore(self):
        ws = self.workspace(); raw = ws.export_case("지난 비교")
        options = deepcopy(ws.store.scenario["options"]); options[0]["loss_percent"] = 4
        ws.store.update({"options": options}, ws.store.version)
        before = deepcopy(ws.store.__dict__); result = ws.compare_case(raw, ws.store.version)
        self.assertEqual(before, ws.store.__dict__)
        self.assertEqual(result["outcomes"][0], {"option_id": "seven_days", "saved_minimum": "3109", "current_minimum": "3174", "difference_per_kg": "65"})
        self.assertTrue(any(x["field"].endswith("loss_percent") for x in result["changes"]))

    def test_equivalent_numeric_text_and_assertion_do_not_count(self):
        ws = self.workspace(); raw = ws.export_case("동일")
        ws.store.scenario["quantity_kg"] = "3000.00"; ws.store.scenario["options"][0]["loss_confirmed_for_period"] = False
        result = ws.compare_case(raw, ws.store.version)
        self.assertFalse([c for c in result["changes"] if not c["field"].startswith("task_context")])

    def test_added_removed_options_not_matched_by_position(self):
        ws = self.workspace(); raw = ws.export_case("이전")
        ws.store.scenario["options"][0]["id"] = "different"
        result = ws.compare_case(raw, ws.store.version)
        self.assertEqual(len(result["outcomes"]), 2)
        self.assertTrue(all(o["difference_per_kg"] is None for o in result["outcomes"]))

    def test_basis_change_suppresses_delta(self):
        ws = self.workspace(); raw = ws.export_case("기준")
        ws.store.scenario.update(price_basis="gross", included_costs=[])
        result = ws.compare_case(raw, ws.store.version)
        self.assertFalse(result["directly_comparable"])
        self.assertIsNone(result["outcomes"][0]["difference_per_kg"])

    def test_missing_inputs_partial_without_fabricated_numbers(self):
        ws = self.workspace(); raw = ws.export_case("기준")
        ws.store.scenario["current_price_per_kg"] = None
        result = ws.compare_case(raw, ws.store.version)
        self.assertEqual(result["review_status"], "PARTIAL")
        self.assertEqual(result["current_recalculated"]["status"], "NEEDS_INPUT")
        self.assertIsNone(result["outcomes"][0]["current_minimum"])

    def test_archived_fake_numbers_never_become_current_output(self):
        ws = self.workspace(); saved = parse_case(ws.export_case("이전"))
        saved["archived_results"] = [{"result": {"minimum_whole_krw_per_kg": "999999999"}}]
        result = compare_cases(saved, ws.store.snapshot(), ws.session.task_context.snapshot(), ws.data_revision, ws.input_context)
        self.assertNotIn("999999999", json.dumps(result))
        self.assertEqual(result["outcomes"][0]["saved_minimum"], "3109")

    def test_data_and_query_changes_are_explicit(self):
        ws = self.workspace(); saved = parse_case(ws.export_case("이전"))
        context = ws.session.task_context.snapshot(); context["shipment_query"] = {"shipment_variety": "노지온주"}
        result = compare_cases(saved, ws.store.snapshot(), context, ["changed"], ws.input_context)
        self.assertTrue(result["data_changed"])
        self.assertTrue(any(c["field"] == "task_context.shipment_query" for c in result["changes"]))

    def test_v1_compatibility(self):
        import hashlib
        ws = self.workspace(); doc = json.loads(ws.export_case("이전")); doc["version"] = 1; doc["payload"].pop("task_context")
        doc["sha256"] = hashlib.sha256(_canonical(doc["payload"])).hexdigest()
        self.assertEqual(ws.compare_case(_canonical(doc), ws.store.version)["status"], "OK")

    def test_bad_file_and_stale_version_keep_previous_reference(self):
        ws = self.workspace(); raw = ws.export_case("이전"); ws.compare_case(raw, ws.store.version)
        old = deepcopy(ws.session.comparison_reference); before = ws.store.snapshot()
        for content, version in [(b"invalid", ws.store.version), (raw, ws.store.version + 1)]:
            self.assertEqual(ws.compare_case(content, version)["status"], "INVALID_INPUT")
        self.assertEqual(old, ws.session.comparison_reference); self.assertEqual(before, ws.store.snapshot())

    def test_missing_reference_and_fake_model_path(self):
        ws = self.workspace(); client = FakeClient([response(call("review_case_changes", {"expected_version": ws.store.version, "finish_turn": True}))])
        ws.session.client = client
        self.assertEqual(ws.run("지난 비교와변경검토")["visible_results"][0]["result"]["status"], "NEEDS_INPUT")
        ws.compare_case(ws.export_case("이전"), ws.store.version)
        ws.session.client = FakeClient([response(call("review_case_changes", {"expected_version": ws.store.version, "finish_turn": True}))])
        self.assertEqual(ws.run("이 파일과비교")["visible_results"][0]["result"]["analysis_type"], "case_changes")
        ws.clear_conversation(False); self.assertIsNone(ws.session.comparison_reference)

    def test_pdf_contains_recalculation_and_changes(self):
        from agents.juneok.pdf_report import render_pdf_report
        from pypdf import PdfReader
        ws = self.workspace(); raw = ws.export_case("이전")
        ws.store.scenario["options"][0]["loss_percent"] = 4
        ws.compare_case(raw, ws.store.version)
        text = "".join(p.extract_text() for p in PdfReader(BytesIO(render_pdf_report(ws.current_entries()))).pages)
        self.assertIn("3,109", text); self.assertIn("3,174", text); self.assertNotIn("| 달라진", text)

    def test_ui_selection_does_not_restore(self):
        from streamlit.testing.v1 import AppTest
        from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
        from streamlit.proto.Common_pb2 import FileURLs
        raw = self.workspace().export_case("기준")
        upload = UploadedFile(UploadedFileRec(file_id="case", name="case.json", type="application/json", data=raw), FileURLs())
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=35).run()
            next(b for b in app.button if b.label == "가상 예시 조건 불러오기").click().run()
            before = app.session_state.workspace.store.snapshot()
            with patch("streamlit.file_uploader", return_value=upload):
                app.run()
                next(b for b in app.button if b.label == "비교 파일 확인").click().run()
                next(b for b in app.button if b.label == "현재 조건과 변경 사항 검토 · 복원하지 않음").click().run()
            self.assertEqual(before, app.session_state.workspace.store.snapshot())
            self.assertEqual(app.session_state.workspace.entries[0]["result"]["analysis_type"], "case_changes")
            self.assertFalse(list(app.exception))
