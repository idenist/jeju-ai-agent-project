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
