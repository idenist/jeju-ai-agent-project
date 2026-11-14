"""Regression for Hub #855 date alignment, stale results, unified JSON and PDF.

All synthetic economics are clearly marked test fixtures; no claim of actual farm profit.
"""
from __future__ import annotations

import importlib
import ast
import json
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.seunghyuk.shipment_timing import analyze_shipment_timing, resolve_pair_dates
from agents.seunghyuk.shipment_timing_bundle import (
    aligned_economic, export_json, export_payload, report_entries,
    shipment_source_is_current, source_fingerprint,
)


DATES = {"shipment_variety": "노지온주", "option_a_date": "2026-11-20", "option_b_date": "2026-11-27"}


def fake_economic(a="2026-11-20", b="2026-11-27"):
    return {
        "analysis_type": "future_pair", "status": "OK", "start_date": "2026-11-17",
        "price_basis": "gross", "quantity_kg": "3000", "interval_days": 7, "minimum_whole_krw_per_kg": "3500",
        "option_a": {"id": "A", "sale_date": a, "delay_days": 3, "price_per_kg": "3000",
                     "proceeds_krw": "8500000", "loss_percent": "1", "remaining_kg": "2970",
                     "daily_storage_cost": "10000", "other_cost_krw": "0", "fee_percent": "5"},
        "options_b": [{"id": "B", "sale_date": b, "delay_days": 10, "price_per_kg": "3600",
                       "difference_vs_a_krw": "700000", "proceeds_krw": "9200000",
                       "loss_percent": "4", "remaining_kg": "2880", "daily_storage_cost": "10000",
                       "other_cost_krw": "0", "fee_percent": "5"}],
        "evidence": {"B": {"summary": "테스트 전용 사용자가 입력한 가정 단가"}},
        "warnings": ["테스트 전용 가정값으로 실제 농가 수입이 아님"],
    }


class BundleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.timing = analyze_shipment_timing(DATES)
        assert cls.timing["status"] == "OK", cls.timing

    def test_economic_only_if_exact_dates_match(self):
        good = fake_economic()
        self.assertIs(aligned_economic(self.timing, good), good)
        self.assertIsNone(aligned_economic(self.timing, fake_economic(b="2026-11-28")))
        self.assertIsNone(aligned_economic(self.timing, {**good, "status": "NEEDS_INPUT"}))
        self.assertIsNone(aligned_economic(self.timing, {**good, "analysis_type": "price_history"}))
        self.assertIsNone(aligned_economic(self.timing, None))
        self.assertEqual(len(report_entries(self.timing, good)), 2)
        self.assertEqual(len(report_entries(self.timing, fake_economic(b="2026-11-28"))), 1)

    def test_json_source_and_economic_not_guessed(self):
        timing_only = json.loads(export_json(self.timing))
        self.assertEqual(timing_only["status"], "SHIPMENT_ONLY")
        self.assertIsNone(timing_only["economic_comparison"])
        combined = json.loads(export_json(self.timing, fake_economic()))
        self.assertEqual(combined["status"], "INTEGRATED")
        self.assertEqual(combined["economic_comparison"]["option_a"]["sale_date"], DATES["option_a_date"])
        self.assertEqual(combined["shipment_analysis"]["source"]["dataset"].startswith("제주데이터허브"), True)
        self.assertFalse(combined["is_future_forecast"])
        self.assertEqual(combined["shipment_analysis"]["paired_year_count"], 7)

    def test_source_hash_blocks_stale(self):
        fingerprint = source_fingerprint()
        self.assertEqual(len(fingerprint[0]), 64)
        self.assertTrue(shipment_source_is_current(self.timing["source"]["data_sha256"]))
        self.assertFalse(shipment_source_is_current("0" * 64))
        self.assertFalse(shipment_source_is_current(None))

    def test_real_pdf_report_renders_both_sections_if_available(self):
        try:
            from agents.juneok.pdf_report import render_pdf_report
        except (ModuleNotFoundError, ImportError):
            self.skipTest("독립 패키지 검사. 대상 저장소에서 실제 PDF 렌더러를 검증합니다")
        entries = report_entries(self.timing, fake_economic())
        pdf = render_pdf_report(entries)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 1500)
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        from io import BytesIO
        reader = PdfReader(BytesIO(pdf))
        self.assertGreaterEqual(len(reader.pages), 1)


class ChatReportPipelineTest(unittest.TestCase):
    """Extract the installed app's pure chat-report functions without starting Streamlit."""

    def test_chat_pdf_json_bundle_dates_and_cache(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        if not app_path.is_file():
            self.skipTest("원격 릴리스 자체 검사. 사용자 저장소에서 app.py 연결을 검사합니다")
        ast_tree = ast.parse(app_path.read_text(encoding="utf-8-sig"))
        functions = [node for node in ast_tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"_chat_report", "_latest_chat_report"}]
        self.assertEqual(len(functions), 2)
        namespace = {"json": json, "_chat_pair": lambda reply: None}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(app_path), "exec"), namespace)
        timing = analyze_shipment_timing(DATES)

        class PairState:
            version = 7
            def current_result(self):
                return fake_economic()

        pair = PairState()
        ws = SimpleNamespace(session=SimpleNamespace(future_pair=pair),
                             store=SimpleNamespace(version=42))
        reply = {"visible_results": [{"tool": "analyze_shipment_timing", "result": timing}]}
        result = namespace["_chat_report"](ws, reply)
        self.assertEqual(result["kind"], "shipment_timing")
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(json.loads(result["json"])["status"], "INTEGRATED")
        messages = [{"reply": {"chat_report": result}}]
        self.assertEqual(namespace["_latest_chat_report"](messages, 42, None, 7)[0], 0)
        self.assertEqual(namespace["_latest_chat_report"](messages, 42, None, 8), (None, None))
        self.assertEqual(namespace["_latest_chat_report"](messages, 41, None, 7), (None, None))
        with patch("agents.seunghyuk.shipment_timing_bundle.source_fingerprint", return_value=("x", "y")):
            self.assertEqual(namespace["_latest_chat_report"](messages, 42, None, 7), (None, None))


class AttrState(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


class Context:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeStreamlit(types.ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.session_state = AttrState()
        self.clicked = True
        self.display = []

    def expander(self, *args, **kwargs):
        return Context()

    def container(self, *args, **kwargs):
        return Context()

    def caption(self, x, *args, **kwargs):
        self.display.append(("caption", x))

    def selectbox(self, *args, **kwargs):
        return "노지온주"

    def date_input(self, *args, **kwargs):
        raise AssertionError("A/B 출하일을 중복 입력하면 안 됩니다")

    def checkbox(self, *args, **kwargs):
        raise AssertionError("날짜 직접 입력 스위치를 남겨두면 안 됩니다")

    def write(self, text, *args, **kwargs):
        self.display.append(("write", text))

    def markdown(self, text, *args, **kwargs):
        self.display.append(("markdown", text))

    def info(self, text, *args, **kwargs):
        self.display.append(("info", text))

    def warning(self, text, *args, **kwargs):
        self.display.append(("warning", text))

    def error(self, text, *args, **kwargs):
        self.display.append(("error", text))

    def columns(self, count, **kwargs):
        return [Context() for _ in range(count)]

    def metric(self, label, value, **kwargs):
        self.display.append(("metric", label))

    def button(self, *args, **kwargs):
        return self.clicked

    def download_button(self, *args, **kwargs):
        self.display.append(("download", args[0]))


class UIPipelineTest(unittest.TestCase):
    def test_auto_date_no_duplicate_and_invalidation(self):
        fake = FakeStreamlit()
        name = "agents.seunghyuk.shipment_timing_ui"
        with patch.dict(sys.modules, {"streamlit": fake}):
            sys.modules.pop(name, None)
            ui = importlib.import_module(name)
            def preview(*args, **kwargs):
                fake.display.append(("combined_export", args[0]))
            with patch.object(ui, "render_integrated_downloads", preview):
                state = {"start_date": "2026-11-17", "option_a": {"delay_days": 3},
                         "option_b": {"delay_days": 10}}
                ui.render_shipment_timing_panel(state)
                self.assertEqual(fake.session_state.timing_result["status"], "OK")
                self.assertEqual(fake.session_state.timing_result["input"]["option_a_date"], "2026-11-20")
                self.assertTrue(any(x[0] == "combined_export" for x in fake.display))
                fake.clicked = False
                fake.display.clear()
                state["option_b"]["delay_days"] = 11
                ui.render_shipment_timing_panel(state)
                self.assertTrue(any(x[0] == "info" for x in fake.display))
                self.assertFalse(any(x[0] == "combined_export" for x in fake.display))
            sys.modules.pop(name, None)

    def test_missing_dates_never_report(self):
        fake = FakeStreamlit()
        name = "agents.seunghyuk.shipment_timing_ui"
        with patch.dict(sys.modules, {"streamlit": fake}):
            sys.modules.pop(name, None)
            ui = importlib.import_module(name)
            ui.render_shipment_timing_panel({"start_date": None})
            self.assertTrue(any(x[0] == "info" for x in fake.display))
            self.assertFalse(any(x[0] == "download" for x in fake.display))
            sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
