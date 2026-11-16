"""Main A/B flow, optional tool focus, and comparable model evaluation contracts."""
from __future__ import annotations
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.tool_policy import select_offered_tools
from scripts.compare_model_runs import compare_runs

ROOT=Path(__file__).resolve().parents[1]

class MVPStage3Contracts(unittest.TestCase):
    def test_ab_is_the_primary_screen_without_deleting_existing_tools(self):
        from streamlit.testing.v1 import AppTest
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            view=AppTest.from_file(str(ROOT/"app.py"),default_timeout=30).run()
        self.assertEqual(list(view.exception), [])
        tab_labels=[item.label for item in view.tabs]
        self.assertGreaterEqual(len(tab_labels),4)
        self.assertEqual(tab_labels[:4],[
            "두 미래 출하안 A/B 비교", "AI 대화", "오늘과 나중", "자료·도구"])
        self.assertTrue(any(x.key=="future_pair_calculate" for x in view.button))
        self.assertTrue(any(x.label=="과거 경락가격 직접 조회" for x in view.button))

    def test_focused_mode_only_when_explicitly_enabled_and_ab_intent(self):
        original=[{"name":x} for x in ("compare_future_pair","analyze_shipment_timing",
           "get_price_history","explain_term","reconcile_settlement","explore_data")]
        kept, active=select_offered_tools(original,enabled=False,
                                          future_pair_request=True,experimental_forecast=False)
        self.assertIs(kept,original)
        self.assertFalse(active)
        reduced, active=select_offered_tools(original,enabled=True,
                                             future_pair_request=True,experimental_forecast=False)
        self.assertTrue(active)
        self.assertEqual({x["name"] for x in reduced},
           {"compare_future_pair","analyze_shipment_timing","get_price_history","explain_term"})
        for pair, experiment in ((False,False),(True,True)):
            out, applied=select_offered_tools(original,enabled=True,
                future_pair_request=pair,experimental_forecast=experiment)
            self.assertIs(out,original)
            self.assertFalse(applied)

    def test_evaluation_compare_rejects_different_data_and_model(self):
        meta={"model":"mock","cases":["INT-01"],"repeats":1,
              "expected_turns_per_repeat":1,"source_sha256":{"data/processed/daily_prices.csv":"a"*64,
              "evals/integration_e2e_cases.json":"b"*64}}
        old={"planned_runs":1,"complete":True,"pass_rate":0.0}
        new={"planned_runs":1,"complete":True,"pass_rate":1.0}
        self.assertEqual(compare_runs((meta,old),(meta,new))["pass_rate_change_percentage_points"],100)
        with self.assertRaises(ValueError):
            compare_runs((meta,old),({**meta,"model":"different"},new))
        with self.assertRaises(ValueError):
            compare_runs((meta,old),({**meta,"source_sha256":{
                **meta["source_sha256"],"data/processed/daily_prices.csv":"c"*64}},new))

if __name__=="__main__":unittest.main()
