"""경매 품종코드 08의 관측된 황금향 표시와 기존 미등록 코드 취급을 검증한다."""

import unittest

from scripts.preprocess_auction import canonical_variety


class AuctionVarietyCode08Tests(unittest.TestCase):
    def test_code_08_is_hwanggeumhyang_even_without_source_label(self):
        self.assertEqual(canonical_variety("08", ""), "황금향")

    def test_existing_codes_remain_unchanged(self):
        self.assertEqual(canonical_variety("01", ""), "조생귤")
        self.assertEqual(canonical_variety("06", ""), "극조생감귤")

    def test_unknown_code_not_silently_mapped_to_hwanggeumhyang(self):
        self.assertEqual(canonical_variety("77", ""), "미분류(77)")
        self.assertEqual(canonical_variety("77", "기타 원천명"), "기타 원천명")


if __name__ == "__main__":
    unittest.main()
