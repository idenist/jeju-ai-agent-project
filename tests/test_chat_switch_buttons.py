"""STEP 1: switching between comparison entry points after a conversation starts."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ChatSwitchButtonsTests(unittest.TestCase):
    def test_both_switch_actions_remain_available_and_reset_active_comparison(self):
        from streamlit.testing.v1 import AppTest

        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_NOT_REAL_KEY"}):
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertTrue(any(b.label == "지금 팔지 더 보관할지 비교" for b in app.button))
            next(b for b in app.button if b.label == "지금 팔지 더 보관할지 비교").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any(b.label == "새 비교 · 두 출하일" for b in app.button))
            self.assertTrue(any(b.label == "새 비교 · 지금 vs 보관" for b in app.button))

            # Simulate an already-established model client. The switch button
            # must never call it just to ask which dates the farmer wants.
            class NotAllowedClient:
                class Responses:
                    @staticmethod
                    def create(**_kwargs):
                        raise AssertionError("Fixed comparison switch must not invoke the LLM")
                responses = Responses()

            app.session_state.workspace.session.client = NotAllowedClient()
            next(b for b in app.button if b.label == "새 비교 · 두 출하일").click().run()
            self.assertFalse(app.exception)
            messages = app.session_state.agent_messages
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["content"], "두 미래 출하일에 감귤을 팔 때를 비교해 주세요.")
            self.assertIn("어느 날짜와 어느 날짜", messages[1]["reply"]["text"])
            self.assertEqual(messages[1]["reply"]["metrics"]["model_requests"], 0)
            self.assertTrue(any(b.label == "새 비교 · 지금 vs 보관" for b in app.button))

            next(b for b in app.button if b.label == "새 비교 · 지금 vs 보관").click().run()
            self.assertFalse(app.exception)
            messages = app.session_state.agent_messages
            self.assertEqual(len(messages), 2)
            self.assertIn("감귤을 지금 팔 때와", messages[0]["content"])
            self.assertIn("감귤 물량", messages[1]["reply"]["text"])
            self.assertEqual(messages[1]["reply"]["metrics"]["model_requests"], 0)


if __name__ == "__main__":
    unittest.main()
