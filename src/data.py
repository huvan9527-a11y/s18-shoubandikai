"""Authenticated JQData adapter; no silent free-data or daily-bar fallback."""
import datetime as dt
import os
import contextlib
import io
import pandas as pd
from . import signals
from .paper import opening_queue

INDEX = '000852.XSHG'
FIELDS = ['open', 'close', 'high_limit', 'low_limit', 'money', 'paused']


class JQData:
    def __init__(self):
        user, password = os.getenv('JQ_USERNAME'), os.getenv('JQ_PASSWORD')
        if not user or not password:
            raise RuntimeError('请在仓库 Actions secrets 配置 JQ_USERNAME / JQ_PASSWORD，并确认 JQData 有当日日线及1分钟权限。')
        import jqdatasdk as jq
        # SDK auth normally prints account status; keep identifiers out of public logs.
        with contextlib.redirect_stdout(io.StringIO()):
            jq.auth(user, password)
        self.jq = jq

    def days(self, **kwargs):
        return [pd.Timestamp(x).date() for x in self.jq.get_trade_days(**kwargs)]

    def prices(self, pool, **kwargs):
        if not pool:
            return pd.DataFrame(columns=['time', 'code'] + list(kwargs.get('fields', FIELDS)))
        frames = []
        fill_paused = kwargs.pop('fill_paused', True)
        for i in range(0, len(pool), 50):
            codes = pool[i:i + 50]
            frame = self.jq.get_price(codes, panel=False, skip_paused=False,
                                      fill_paused=fill_paused, **kwargs)
            if frame is None or frame.empty or not {'time', 'code'}.issubset(frame.columns):
                raise ValueError('JQData returned empty or invalid data')
            if not set(codes).issubset(set(frame.code)):
                raise ValueError('JQData omitted requested stock(s)')
            frames.append(frame)
        return pd.concat(frames, ignore_index=True)

    def prepare(self, date, account, cfg):
        previous = self.days(end_date=date, count=2)
        if len(previous) != 2 or previous[-1] != date:
            raise ValueError('Target is not an exchange trading day')
        t = previous[0]
        calendar = self.days(end_date=t, count=40)
        if len(calendar) != 40:
            raise ValueError('Temperature warmup unavailable')
        ice_hist = account['ice_hist']
        latest_dn = None
        for day in calendar:
            key = day.isoformat()
            if key in ice_hist and day != t:
                continue
            pool = list(self.jq.get_index_stocks(INDEX, date=day))
            df = self.prices(pool, end_date=day, count=2, frequency='daily',
                             fields=['close', 'high_limit', 'low_limit', 'paused'], fq='pre')
            dates = sorted(pd.to_datetime(df.time).dt.date.unique())
            if len(dates) != 2 or dates[-1] != day:
                raise ValueError('Temperature daily data stale')
            stamp = pd.to_datetime(df.time).dt.date
            ice, latest_dn = signals.ice_day(df[stamp == dates[0]], df[stamp == day])
            ice_hist[key] = ice
        run = 0
        for day in reversed(calendar):
            if not ice_hist[day.isoformat()]:
                break
            run += 1
        h = self.prices([INDEX], end_date=t, count=20, frequency='daily', fields=['close'], fq='pre')
        if len(h) != 20 or h.close.isna().any() or pd.Timestamp(h.time.iloc[-1]).date() != t:
            raise ValueError('Index MA20 unavailable or stale')
        ma20 = float(h.close.iloc[-1] / h.close.mean() - 1)
        ice = ice_hist[t.isoformat()]
        temp = dict(ICE=ice, RUN=run, MA20=ma20, M_DN=latest_dn,
                    DEEP=bool(ice and (run >= 2 or ma20 <= -.04)),
                    FB_OK=bool(ma20 < 0 and latest_dn < .03), LB_OK=bool(latest_dn < .03))
        pool = list(self.jq.get_index_stocks(INDEX, date=t))
        all_sec = self.jq.get_all_securities(['stock'], date=t)
        if all_sec is None or all_sec.empty or not set(pool).issubset(all_sec.index):
            raise ValueError('Security metadata incomplete')
        st = signals.checked_st(self.jq.get_extras('is_st', pool, start_date=t, end_date=t, df=True), pool)
        cutoff = t - dt.timedelta(days=250)
        pool = [s for s in pool if not st[s] and '退' not in all_sec.loc[s, 'display_name']
                and pd.Timestamp(all_sec.loc[s, 'start_date']).date() <= cutoff]
        hist = self.prices(pool, end_date=t, count=61, frequency='daily', fields=FIELDS, fq='pre', fill_paused=False)
        raw = self.prices(pool, end_date=t, count=1, frequency='daily', fields=['close'], fq=None)
        flags = signals.stock_flags(hist, raw.set_index('code').close.to_dict(), t)
        return signals.candidates(flags, temp, all_sec.display_name.to_dict(), cfg['min_day_money']), temp

    def session(self, date, candidates, positions, cfg):
        codes = sorted(set(c['code'] for c in candidates) | set(positions))
        if not codes:
            return {}, {}
        daily = self.prices(codes, end_date=date, count=1, frequency='daily',
                            fields=FIELDS + ['factor'], fq=None)
        if (pd.to_datetime(daily.time).dt.date != date).any():
            raise ValueError('Daily data does not reach target date; check JQData entitlement/delay')
        st = signals.checked_st(self.jq.get_extras('is_st', codes, start_date=date, end_date=date, df=True), codes)
        sec = self.jq.get_all_securities(['stock'], date=date)
        marks = {}
        for _, row in daily.iterrows():
            code = row.code
            if code not in sec.index:
                raise ValueError('Missing target-date security metadata')
            marks[code] = {f: float(row[f]) for f in FIELDS + ['factor']}
            marks[code].update(is_st=bool(st[code]), name=sec.loc[code, 'display_name'])
            if code in positions:
                previous_factor = positions[code].get('factor')
                if previous_factor is not None and abs(previous_factor - row.factor) > 1e-8:
                    raise ValueError('持仓发生除权/分红，暂停账本更新，需核对公司行为：' + code)
        qualified = opening_queue(candidates, marks, cfg)
        minute_codes = sorted(set(c['code'] for c in qualified) | {s for s in positions if not marks[s]['paused']})
        bars = self.prices(minute_codes, start_date=str(date) + ' 09:30:00',
                           end_date=str(date) + ' 15:00:00', frequency='1m',
                           fields=['open', 'high', 'low', 'close', 'money', 'volume'], fq=None)
        out = {s: {} for s in minute_codes}
        for _, row in bars.iterrows():
            stamp = pd.Timestamp(row.time)
            if stamp.date() != date:
                raise ValueError('Minute data wrong date')
            tm = stamp.strftime('%H:%M')
            if tm in out[row.code]:
                raise ValueError('Duplicate minute data')
            out[row.code][tm] = {f: float(row[f]) for f in ['open', 'high', 'low', 'close', 'money', 'volume']}
        for c in qualified:
            if any(f'09:{i:02d}' not in out.get(c['code'], {}) for i in range(31, 37)):
                raise ValueError('Buy minutes incomplete: ' + c['code'])
        for code in positions:
            if not marks[code]['paused'] and any(f'14:{i:02d}' not in out.get(code, {}) for i in range(50, 58)):
                raise ValueError('Held-stock exit minutes incomplete: ' + code)
        return marks, out
