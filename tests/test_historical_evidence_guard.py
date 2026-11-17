from datetime import date
import unittest
from unittest.mock import patch

from agents.agent import AgentSession
from agents.evidence_guard import rejected_evidence_claim
from services.shipment_timing import analyze_shipment_timing
from test_juneok_harness import FakeClient, response


class HistoricalEvidenceGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = analyze_shipment_timing({
            "shipment_variety": "노지온주", "option_a_date": "2026-11-23", "option_b_date": "2026-11-30"
        })
        cls.entry = {"tool": "analyze_shipment_timing", "result": cls.result}

    def test_lookup_claims_bind_year_ratio_and_mass_to_the_result(self):
        question = "방금 조회한 출하량 결과 알려줘"
        for text, expected in [
            ("2018~2024년, B안 출하량이 더 많았던 해는 7개 중 5개입니다.", None),
            ("B안 출하량이 더 많았던 해는 7년 중 5년(71.4%)입니다.", None),
            ("B안 출하량이 더 많았던 해는 7년 중 5년(81.4%)입니다.", "sample_count"),
            ("2025년도 포함해 8개년 출하량을 조회했습니다.", "observed_year"),
            ("6개년 기록입니다.", "sample_count"),
            ("A안 출하량이 더 많았던 해는 7개 중 5개입니다.", "sample_count"),
            ("B안 출하량이 더 많았던 것은 7개년입니다.", "sample_count"),
            ("B안 출하량 중앙값은 13,588.85톤입니다.", None),
            ("B안 출하량 중앙값은 13,588,850kg입니다.", None),
            ("B안 출하량 중앙값은 13,336.59톤입니다.", "observed_mass"),
            ("B안 출하량 중앙값은 99,000톤입니다.", "observed_mass"),
            ("B안 출하량 중앙값은 99,000톤이고 상자 포장은 그대로입니다.", "observed_mass"),
            ("5kg 상자를 사용하고 B안 출하량 중앙값은 99,000톤입니다.", "observed_mass"),
            ("B안 출하량 중앙값은 99,000kg, 포장 상자는 5kg입니다.", "observed_mass"),
            ("5kg 상자이며 B안 출하량 중앙값은 13,588.85톤입니다.", None),
            ("상자 포장 규격은 **5kg**이며, B안 출하량 중앙값은 **13,588.85톤**입니다.", None),
            ("상자는 **5kg**이고 비교한 B안 출하량 중앙값은 **13,588.85톤**입니다.", None),
            ("포장 규격은 **5kg 상자**이며, 노지온주 **B안(2026년 11월 30일 주변 7일)**의 과거 출하량 중앙값은 **13,588.85톤**입니다.", None),
            ("포장 규격은 5kg 상자이며, B안(2026년 11월 30일 전후 7일)의 과거 출하량 중앙값은 99,000톤입니다.", "observed_mass"),
            ("실제로 2026년 11월 30일에 신고된 출하량은 13,588.85톤입니다.", "observed_year"),
            ("비교 물량은 3톤이고 B안 출하량 중앙값은 13,588.85톤, 상자 무게는 5kg입니다.", None),
            ("이 자료로 가격 상승 확률은 71.4%입니다.", "historical_probability"),
            ("이 자료의 가격 상승 확률은 5/7입니다.", "historical_probability"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(rejected_evidence_claim(text, question, self.entry), expected)

    def test_explicit_price_pair_count_uses_the_verified_table(self):
        entry = {"tool": "get_price_history", "result": {"clean_pairs": 20}}
        question = "조회한 가격쌍 몇 개예요?"
        self.assertIsNone(rejected_evidence_claim("가격쌍 20쌍이에요.", question, entry))
        self.assertEqual(rejected_evidence_claim("21쌍이에요.", question, entry), "sample_count")

    def test_failed_new_price_lookup_cannot_anchor_an_old_shipment_amount(self):
        entry = {"tool": "get_price_history", "result": {"status": "NO_DATA"}}
        question = "지금 조회 결과와 5kg 상자를 함께 적어줘."
        self.assertEqual(rejected_evidence_claim("5kg 상자, 출하량 중앙값 13,588.85톤입니다.", question, entry),
                         "missing_observation")
        self.assertIsNone(rejected_evidence_claim("5kg 상자에 해당하는 연도별 관측 자료가 없습니다.", question, entry))

    @patch("services.data_refresh.kst_today", return_value=date(2026, 11, 18))
    def test_changed_source_cannot_republish_remembered_shipment_amount(self, _today):
        client = FakeClient([response(text="5kg 상자이며 B안 출하량 중앙값은 13,588.85톤입니다.")])
        session = AgentSession(client=client)
        with patch("agents.agent.lookup_revision", side_effect=[("old",), ("new",)]):
            session.run("노지온주 11월 23일과 30일 주변 과거 출하량을 봐줘.")
            reply = session.run("방금 조회한 결과와 5kg 상자를 같이 알려줘.")
        self.assertEqual(reply["metrics"]["unsupported_evidence_blocked"], "missing_observation")
        self.assertNotIn("13,588.85", reply["text"])

    @patch("services.data_refresh.kst_today", return_value=date(2026, 11, 18))
    def test_remembered_csv_lookup_replaces_an_unverified_model_claim(self, _today):
        client = FakeClient([response(text="2025년을 포함한 8개년이고 중앙값은 99,000톤입니다.")])
        session = AgentSession(client=client)
        first = session.run("노지온주 11월 23일과 30일 주변 과거 출하량을 봐줘.")
        self.assertEqual(first["metrics"]["model_requests"], 0)
        reply = session.run("방금 조회한 출하량은 2025년까지 8개년이라고 적어 줘.")
        self.assertEqual(reply["metrics"]["unsupported_evidence_blocked"], "observed_year")
        self.assertIn("2018~2024", reply["text"])
        self.assertIn("13,588.85톤", reply["text"])
        self.assertNotIn("99,000톤", reply["text"])
        self.assertEqual(len(client.requests), 1)
