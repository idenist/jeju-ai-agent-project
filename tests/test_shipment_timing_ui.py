"""Smoke-test the read-only panel without requiring Streamlit to be installed."""
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class AttrState(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class Context:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeStreamlit(types.ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.session_state = AttrState()
        self.output = []
        self.clicked = True

    def expander(self, *args, **kwargs):
        return Context()

    def caption(self, *args, **kwargs):
        pass

    def selectbox(self, *args, **kwargs):
        return "노지온주"

    def checkbox(self, *args, **kwargs):
        return False

    def columns(self, count, **kwargs):
        return [Context() for _ in range(count)]

    def button(self, *args, **kwargs):
        return self.clicked

    def metric(self, label, value, *args, **kwargs):
        self.output.append((label, value))

    def markdown(self, body, *args, **kwargs):
        self.output.append(("markdown", body))

    def write(self, body):
        self.output.append(("write", body))

    def info(self, body):
        self.output.append(("info", body))

    def warning(self, body):
        self.output.append(("warning", body))

    def error(self, body):
        self.output.append(("error", body))

    def download_button(self, *args, **kwargs):
        self.output.append(("download", args))


class TimingUISmoke(unittest.TestCase):
    def test_pair_dates_and_no_stale_display(self):
        fake = FakeStreamlit()
        data_root = Path(__file__).resolve().parents[1]
        with patch.dict(sys.modules, {"streamlit": fake}):
            name = "agents.seunghyuk.shipment_timing_ui"
            sys.modules.pop(name, None)
            module = importlib.import_module(name)
            # The package root is the directory containing the two already-preprocessed files.
            from agents.seunghyuk import shipment_timing
            original = shipment_timing.ROOT
            shipment_timing.ROOT = data_root
            try:
                state = {"start_date": "2026-11-17", "option_a": {"delay_days": 3},
                         "option_b": {"delay_days": 10}}
                module.render_shipment_timing_panel(state)
                self.assertEqual(fake.session_state.timing_result["status"], "OK")
                self.assertEqual(fake.session_state.timing_result["input"]["option_a_date"], "2026-11-20")
                self.assertTrue(any(label == "download" for label, _ in fake.output))
                fake.clicked = False
                fake.output.clear()
                state["option_b"]["delay_days"] = 11
                module.render_shipment_timing_panel(state)
                self.assertFalse(any(label == "download" for label, _ in fake.output))
                self.assertTrue(any(label == "info" for label, _ in fake.output))
            finally:
                shipment_timing.ROOT = original
                sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
