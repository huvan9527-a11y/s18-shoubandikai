import copy
import unittest
from datetime import datetime, timedelta
from src import paper


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = paper.DEFAULTS.copy()
        self.account = paper.new_account('2026-10-08')
        self.candidate = dict(code='000001.XSHE', name='测试', component='FB_dip',
                              pri=0, prev_close=10.0, money=2e8,
                              open=9.5, high_limit=11.0, low_limit=9.0, gap=-0.05)

    def test_buy_budget_and_fees_never_overdraw(self):
        order = paper.make_buy(self.account, self.candidate, 9.5, 2e6, '09:32', self.cfg)
        self.assertEqual(order['amount'], 10500)
        self.assertLessEqual(order['amount'] * order['limit'] + 25, 100000)
        bar = dict(open=9.5, high=9.52, low=9.48, close=9.5, money=2e6, volume=200000)
        trade = paper.match(self.account, order, bar, '2026-10-08', '09:33', self.cfg)
        self.assertIsNotNone(trade)
        self.assertGreaterEqual(self.account['cash'], 0)
        self.assertEqual(len(self.account['positions']), 1)

    def test_no_fill_if_next_minute_does_not_reach_limit(self):
        order = paper.make_buy(self.account, self.candidate, 9.5, 2e6, '09:32', self.cfg)
        before = copy.deepcopy(self.account)
        bar = dict(open=9.6, high=9.7, low=9.59, close=9.65, money=2e6, volume=200000)
        self.assertIsNone(paper.match(self.account, order, bar, '2026-10-08', '09:33', self.cfg))
        self.assertEqual(before, self.account)

    def test_one_price_limit_down_sell_is_not_filled(self):
        self.account['positions']['000001.XSHE'] = dict(amount=1000, entry_date='2026-10-08',
                                                     cost=9505, mark=9.5, name='测试')
        order = dict(code='000001.XSHE', side='sell', amount=1000, limit=9.0, low_limit=9.0,
                     high_limit=11.0, submitted='14:50', component='FB_dip')
        bar = dict(open=9., high=9., low=9., close=9., money=1e8, volume=1e7)
        self.assertIsNone(paper.match(self.account, order, bar, '2026-10-09', '14:51', self.cfg))
        self.assertIn('000001.XSHE', self.account['positions'])

    def test_t_plus_one(self):
        self.account['positions']['000001.XSHE'] = dict(amount=1000, entry_date='2026-10-08',
                                                     cost=9505, mark=9.5, name='测试')
        order = dict(code='000001.XSHE', side='sell', amount=1000, limit=9.4, low_limit=9.,
                     high_limit=11., submitted='14:50', component='FB_dip')
        bar = dict(open=9.5, high=9.6, low=9.5, close=9.6, money=1e8, volume=1e7)
        self.assertIsNone(paper.match(self.account, order, bar, '2026-10-08', '14:51', self.cfg))
        trade = paper.match(self.account, order, bar, '2026-10-09', '14:51', self.cfg)
        self.assertAlmostEqual(trade['tax'], trade['gross'] * .0005, places=2)
        self.assertNotIn('000001.XSHE', self.account['positions'])

    def test_partial_fill_respects_volume_and_cancels_remainder(self):
        order = paper.make_buy(self.account, self.candidate, 9.5, 2e6, '09:32', self.cfg)
        bar = dict(open=9.5, high=9.52, low=9.48, close=9.5, money=95000, volume=10000)
        trade = paper.match(self.account, order, bar, '2026-10-08', '09:33', self.cfg)
        self.assertEqual(trade['amount'], 1000)
        self.assertEqual(self.account['positions']['000001.XSHE']['amount'], 1000)

    def test_board_lots(self):
        self.assertEqual(paper.lot_amount(199, '688001.XSHG'), 0)
        self.assertEqual(paper.lot_amount(251, '688001.XSHG'), 251)
        self.assertEqual(paper.lot_amount(251, '000001.XSHE'), 200)

    def test_no_chasing_past_open_cap(self):
        self.assertIsNone(paper.make_buy(self.account, self.candidate, 9.54, 2e6, '09:32', self.cfg))


class ReplayTests(unittest.TestCase):
    def fixture(self):
        candidate = dict(code='000001.XSHE', name='测试', component='FB_dip', pri=0,
                         prev_close=10., money=2e8)
        daily = {'000001.XSHE': dict(open=9.5, close=9.6, high_limit=11., low_limit=9., paused=0, is_st=False, name='测试')}
        bars = {}
        now = datetime(2026, 10, 8, 9, 31)
        for k in range(6):
            t = (now + timedelta(minutes=k)).strftime('%H:%M')
            bars[t] = dict(open=9.5, high=9.52, low=9.48, close=9.5, money=2e6, volume=200000)
        for k in range(8):
            t = (datetime(2026, 10, 8, 14, 50) + timedelta(minutes=k)).strftime('%H:%M')
            bars[t] = dict(open=9.6, high=9.62, low=9.58, close=9.6, money=2e6, volume=200000)
        return [candidate], daily, {'000001.XSHE': bars}

    def test_duplicate_day_is_no_op(self):
        account = paper.new_account('2026-10-08')
        c, d, m = self.fixture()
        paper.replay_day(account, '2026-10-08', c, d, m, paper.DEFAULTS)
        before = copy.deepcopy(account)
        paper.replay_day(account, '2026-10-08', c, d, m, paper.DEFAULTS)
        self.assertEqual(before, account)
        self.assertEqual(len(account['trades']), 1)
        self.assertIn('000001.XSHE', account['positions'])
        paper.replay_day(account, '2026-10-09', [], d, m, paper.DEFAULTS)
        self.assertEqual(len(account['trades']), 2)
        self.assertFalse(account['positions'])

    def test_future_bar_cannot_change_buy_decision(self):
        c, d, m = self.fixture()
        a, b = paper.new_account('2026-10-08'), paper.new_account('2026-10-08')
        paper.replay_day(a, '2026-10-08', c, d, m, paper.DEFAULTS)
        modified = copy.deepcopy(m)
        modified['000001.XSHE']['14:50']['close'] = 50
        modified['000001.XSHE']['14:50']['high'] = 50
        paper.replay_day(b, '2026-10-08', c, d, modified, paper.DEFAULTS)
        self.assertEqual(a['trades'], b['trades'])

    def test_missing_held_data_leaves_account_unchanged(self):
        c, d, m = self.fixture()
        a = paper.new_account('2026-10-08')
        paper.replay_day(a, '2026-10-08', c, d, m, paper.DEFAULTS)
        before = copy.deepcopy(a)
        with self.assertRaises(ValueError):
            paper.replay_day(a, '2026-10-09', [], d, {}, paper.DEFAULTS)
        self.assertEqual(a, before)

    def test_max_two_slots_even_with_simultaneous_orders(self):
        c, d, m = self.fixture()
        for code in ['000002.XSHE', '000003.XSHE']:
            c.append(dict(c[0], code=code))
            d[code] = d['000001.XSHE'].copy()
            m[code] = copy.deepcopy(m['000001.XSHE'])
        a = paper.new_account('2026-10-08')
        paper.replay_day(a, '2026-10-08', c, d, m, paper.DEFAULTS)
        self.assertEqual(len(a['positions']), 2)
        self.assertEqual(len(a['trades']), 2)


if __name__ == '__main__':
    unittest.main()
