"""Regression: earlier ordinary chat replies stay readable, not hidden in accordions."""
import unittest
from unittest.mock import patch


class PriorChatVisibilityTests(unittest.TestCase):
    def test_previous_missing_input_prompt_has_no_stale_warning(self):
        from app import _show_agent_reply
        response = {"text": "이전 출하 조건에서 확인한 가격입니다.",
                    "state": {"version": 1},
                    "visible_results": [{"tool": "compare_shipment_options",
                                         "result": {"status": "NEEDS_INPUT"}}]}
        with patch("app.st.expander") as expander, patch("app.st.markdown") as markdown, patch("app.st.caption") as caption:
            _show_agent_reply(response, 3)
        expander.assert_not_called()
        markdown.assert_called_once_with(response["text"])
        caption.assert_not_called()

    def test_current_reply_keeps_normal_display_without_stale_caption(self):
        from app import _show_agent_reply
        with patch("app.st.expander") as expander, patch("app.st.markdown") as markdown, patch("app.st.caption") as caption:
            _show_agent_reply({"text": "최신 안내", "state": {"version": 3}}, 3)
        expander.assert_not_called()
        caption.assert_not_called()
        markdown.assert_called_once_with("최신 안내")

    def test_previous_future_pair_result_remains_folded_to_avoid_stale_financial_advice(self):
        from app import _show_agent_reply
        reply = {"text": "이전 A/B 계산", "state": {"version": 3},
                 "visible_results": [{"tool": "compare_future_pair", "result": {"status": "OK"}}]}
        with patch("app.st.expander") as expander, patch("app.st.markdown") as markdown, patch("app.st.caption") as caption:
            _show_agent_reply(reply, 3, past_future_pair=True)
        expander.assert_called_once_with("이전 두 출하일 비교 응답")
        markdown.assert_called_once_with("이전 A/B 계산")
        caption.assert_not_called()

    def test_unsaved_card_edit_displays_visible_warning(self):
        from app import _show_agent_reply
        with patch("app.st.expander") as expander, patch("app.st.markdown") as markdown, patch("app.st.caption") as caption:
            _show_agent_reply({"text": "수정 전 결과", "state": {"version": 3}}, 3, draft_pending=True)
        expander.assert_not_called()
        markdown.assert_called_once_with("수정 전 결과")
        self.assertIn("수정 전 기준", caption.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
