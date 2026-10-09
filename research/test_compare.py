import datetime as dt
import unittest
import pandas as pd
from research.compare import portfolio

class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.days=[dt.date(2025,10,9)+dt.timedelta(days=x) for x in range(4)]
        self.code='000420.XSHE'
        self.raw={self.code:pd.DataFrame([dict(open=9.,close=10.,high_limit=11.,low_limit=8.,
                                             factor=1.,paused=0) for _ in self.days],index=self.days)}
        self.events=[dict(date=str(self.days[0]),code=self.code,pri=0,gap=-.03,money=2e8,component='FB_dip')]

    def test_third_trading_day_counts_entry_day(self):
        r=portfolio(self.events,self.raw,self.days,'open',2,'close')
        self.assertEqual(r['fills'][0]['exit_date'],str(self.days[2]))

    def test_t_plus_one_for_close_entry(self):
        r=portfolio(self.events,self.raw,self.days,'close',1,'open')
        self.assertEqual(r['fills'][0]['exit_date'],str(self.days[1]))

    def test_down_limit_defers_exit(self):
        self.raw[self.code].loc[self.days[1],'open']=8.
        r=portfolio(self.events,self.raw,self.days,'open',1,'open')
        self.assertEqual(r['fills'][0]['exit_date'],str(self.days[2]))

    def test_close_limit_up_does_not_fill(self):
        self.raw[self.code].loc[self.days[0],'close']=11.
        r=portfolio(self.events,self.raw,self.days,'close',1,'open')
        self.assertEqual(r['closed_trades'],0)
        self.assertEqual(r['final_equity'],200000.)

    def test_missing_future_stays_open(self):
        r=portfolio(self.events,self.raw,self.days[:1],'open',1,'open')
        self.assertEqual(r['closed_trades'],0)
        self.assertEqual(r['open_positions'],1)

if __name__=='__main__':unittest.main()
