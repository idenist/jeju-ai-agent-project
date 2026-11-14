import unittest
from datetime import date
from pathlib import Path
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_market_signal import Summary, ParsedData, history_window, get_features, profit, log_change, split_season

class TestMarketSignal(unittest.TestCase):
    def setUp(self):
        self.spec=('110001','01','11','184','상자','5.000','경매')
        self.parsed=ParsedData(spec_days={},market_days={},variety_days={},audit={})
        for d in range(1,21):
            t=date(2025,11,d)
            self.parsed.spec_days[('2025-2026',t,self.spec)]=Summary(kg=100,amount=100000,count=6)
            self.parsed.market_days[('2025-2026',t,'110001')]=Summary(kg=1000,amount=4000000,count=70)
            self.parsed.variety_days[('2025-2026',t,'110001','01')]=Summary(kg=100,amount=100000,count=6)

    def test_future_data_does_not_change_features(self):
        d=date(2025,11,15)
        before=get_features(self.parsed,'2025-2026',d,self.spec)
        later=date(2025,11,16)
        self.parsed.spec_days[('2025-2026',later,self.spec)] = Summary(kg=123,amount=98700000,count=100)
        self.parsed.market_days[('2025-2026',later,'110001')]=Summary(kg=100,amount=7000000,count=70)
        self.parsed.variety_days[('2025-2026',later,'110001','01')]=Summary(kg=100,amount=9000000,count=70)
        after=get_features(self.parsed,'2025-2026',d,self.spec)
        self.assertEqual(before,after)

    def test_history_window_rejects_future(self):
        with self.assertRaises(ValueError):
            history_window(self.parsed.market_days,'2025-2026',date(2025,11,15),('110001',),-1,1)

    def test_unknown_and_low_sample_not_quality_ok(self):
        self.assertFalse(Summary(kg=100,amount=100000,count=4).quality_ok)
        self.assertFalse(Summary(kg=100,amount=100000,count=5,grade_known=False).quality_ok)
        self.assertTrue(Summary(kg=100,amount=100000,count=5).quality_ok)

    def test_choice_profit_consistent(self):
        self.assertGreater(profit(5000,3),profit(1000,14))
        self.assertEqual(log_change(100,100),0)

    def test_source_season(self):
        self.assertEqual(split_season('경매_raw/2024-2025/a.csv'),'2024-2025')
        self.assertIsNone(split_season('2024other/a.csv'))

if __name__=='__main__':unittest.main()
