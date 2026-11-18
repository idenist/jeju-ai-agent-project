import unittest

from agents.future_pair_state import FuturePairState, future_pair_feedback
from presenters.data_basis import data_basis_line

LARGE = {"variety": "조생귤", "grade": "특", "size": "소", "package": "상자", "package_kg": "5"}
SMALL = {"variety": "하우스감귤", "grade": "특", "size": "60내", "package": "상자", "package_kg": "3"}


def chat_preview(filters, a_days=3, b_days=10):
    pair = FuturePairState()
    option = {"loss_percent": None, "daily_storage_cost": None, "other_cost_krw": None, "fee_percent": 5}
    result = pair.apply("new", pair.version, {
        "quantity": 3000, "price_basis": "gross", "start_date": "2026-11-17", "price_a": 2500,
        "option_a": {**option, "delay_days": a_days}, "option_b": {**option, "delay_days": b_days},
        "price_mode": "history", "history_filters": filters})
    return result, future_pair_feedback(result)


class PreviewDataBasisTest(unittest.TestCase):
    """비용 입력 전 미리보기에도 어떤 자료로 계산했는지 한 줄로 보여야 한다."""

    def test_chat_preview_shows_source_period_and_pairs(self):
        result, text = chat_preview(LARGE)
        self.assertEqual(result["history_preview"]["clean_pairs"], 20)
        self.assertIn("서울가락 경매 기본 자료 · 2026-10-01~11-03 관측 · 같은 규격 7일 20쌍", text)

    def test_small_sample_line_uses_its_own_count(self):
        _, text = chat_preview(SMALL)
        self.assertIn("같은 규격 7일 3쌍", text)

    def test_season_file_and_missing_period(self):
        preview = {"interval_days": 14, "clean_pairs": 15,
                   "conditions": {"market_code": "110001", "trade_type": "경매"},
                   "source": {"comparison_file": "data\\seasons\\2024-2025\\price_comparison_availability.csv"}}
        # 관측기간이 없으면 지어내지 않고 그 부분만 뺀다.
        self.assertEqual(data_basis_line(preview), "서울가락 경매 2024-2025 시즌 자료 · 같은 규격 14일 15쌍")

    def test_period_across_years_keeps_full_end_date(self):
        preview = {"interval_days": 7, "clean_pairs": 28, "conditions": {},
                   "source": {"observed_period": ["2026-12-20", "2027-01-31"]}}
        self.assertEqual(data_basis_line(preview), "기본 자료 · 2026-12-20~2027-01-31 관측 · 같은 규격 7일 28쌍")


if __name__ == "__main__":
    unittest.main()
