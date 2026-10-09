"""Credential-free public data. Missing historical metadata is never silently exact."""
import datetime as dt
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO, BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import requests
from . import signals
from .paper import band, opening_queue

ROOT = Path(__file__).resolve().parents[1]
INDEX = '000852.XSHG'

def symbol(code):
    s = code[:6]
    return ('sh' if s.startswith('6') or code == INDEX else 'sz') + s

def code_id(s):
    return s + ('.XSHG' if s.startswith('6') else '.XSHE')

def cent(x):
    return float(Decimal(str(x)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))

def get(url, **kwargs):
    error = None
    for _ in range(3):
        try:
            r = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'}, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            error = e
    raise error

class PublicData:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.cache = self.root / 'cache/public'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.today = dt.datetime.now(ZoneInfo('Asia/Shanghai')).date()
        self.frames = {}
        self.names = {}
        self.pool = None
        self.errors = {}

    def daily(self, code):
        if code in self.frames:
            return self.frames[code]
        path = self.cache / f'{symbol(code)}-{self.today}.json'
        if path.exists():
            payload = json.loads(path.read_text())
        elif code == INDEX:
            r = get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get', params={
                'param': f'sh000852,day,{self.today-dt.timedelta(days=400)},{self.today},320,'})
            rows = r.json()['data']['sh000852']['day']
            payload = {'rows': [dict(date=x[0], open=x[1], close=x[2], high=x[3], low=x[4], volume=x[5], amount=0) for x in rows], 'factors': []}
        else:
            from akshare.stock.cons import zh_sina_a_stock_hist_url, hk_js_decode, zh_sina_a_stock_qfq_url
            from py_mini_racer import MiniRacer
            r = get(zh_sina_a_stock_hist_url.format(symbol(code)))
            js = MiniRacer()
            js.eval(hk_js_decode)
            rows = js.call('d', r.text.split('=', 1)[1].split(';')[0].replace('"', ''))
            factors = get(zh_sina_a_stock_qfq_url.format(symbol(code))).text
            factors = json.JSONDecoder().raw_decode(factors.split('=', 1)[1].lstrip())[0]['data']
            payload = {'rows': rows, 'factors': factors}
        if not path.exists():
            path.write_text(json.dumps(payload), encoding='utf-8')
        df = pd.DataFrame(payload['rows'])
        df['time'] = pd.to_datetime(df['date'], utc=True).dt.tz_localize(None).dt.normalize()
        df = df.sort_values('time').drop_duplicates('time').reset_index(drop=True)
        self.listed = getattr(self, 'listed', {})
        self.listed[code] = df.time.iloc[0].date()
        for f in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            df[f] = pd.to_numeric(df[f], errors='raise')
        if df[['open', 'high', 'low', 'close', 'amount', 'volume']].isna().any().any():
            raise ValueError('Non-finite history: ' + code)
        df['money'] = df.amount
        df['paused'] = (df.volume <= 0).astype(int)
        df['code'] = code
        df['factor'] = 1.
        for f in sorted(payload['factors'], key=lambda x: x['d'], reverse=True):
            df.loc[df.time < pd.Timestamp(f['d']), 'factor'] = 1 / float(f['f'])
        prev = df.close.shift(1) * df.factor.shift(1) / df.factor
        df['high_limit'] = prev.map(lambda p: cent(p * (1 + band(code))) if pd.notna(p) else float('nan'))
        df['low_limit'] = prev.map(lambda p: cent(p * (1 - band(code))) if pd.notna(p) else float('nan'))
        # Dividend/split dates cannot use the prior raw close to infer exchange limits.
        df['corporate_action'] = df.factor.diff().abs().fillna(0) > 1e-10
        self.frames[code] = df
        return df

    def days(self, start_date=None, end_date=None, count=None):
        df = self.daily(INDEX)
        dates = [x.date() for x in df.time]
        dates = [x for x in dates if (start_date is None or x >= pd.Timestamp(start_date).date()) and (end_date is None or x <= pd.Timestamp(end_date).date())]
        return dates[-count:] if count else dates

    def constituents(self):
        if self.pool is not None:
            return self.pool
        path = self.root / 'state/metadata' / f'{self.today}.json'
        if path.exists():
            data = json.loads(path.read_text())
        else:
            def page(n):
                r = get(f'https://vip.stock.finance.sina.com.cn/corp/view/vII_NewestComponent.php?page={n}&indexid=000852')
                r.encoding = 'gb2312'
                tables = pd.read_html(StringIO(r.text), header=1)
                table = next(t for t in tables if '品种代码' in t.columns)
                return [dict(code=code_id(str(row['品种代码']).zfill(6)), name=str(row['品种名称']), inclusion=str(row['纳入日期'])) for _, row in table.iterrows()]
            try:
                response = get('https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000852cons.xls')
                table = pd.read_excel(BytesIO(response.content), dtype=str)
                table.columns = ['date', 'index', 'index_name', 'index_en', 'code', 'name', 'name_en', 'exchange', 'exchange_en']
                data = {code_id(str(row.code).zfill(6)): dict(code=code_id(str(row.code).zfill(6)), name=row['name'], inclusion='1900-01-01', source_date=row['date']) for _, row in table.iterrows()}
            except Exception as e:
                print(f'Official constituent source failed: {type(e).__name__}; validating fallback', flush=True)
                first = page(1)
                with ThreadPoolExecutor(max_workers=4) as executor:
                    pages = list(executor.map(page, range(2, 26)))
                data = {x['code']: x for x in first + [x for p in pages for x in p]}
            if len(data) != 1000:
                raise ValueError(f'Index constituents incomplete: {len(data)}/1000')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        self.pool = data
        self.names = {k: v['name'] for k, v in data.items()}
        return data

    def load_pool(self):
        pool = self.constituents()
        def load(code):
            try:
                self.daily(code)
                self.errors.pop(code, None)
            except Exception as e:
                self.errors[code] = f'{type(e).__name__}: {str(e)[:150]}'
                if len(self.errors) <= 5:
                    print(f'{code}: {self.errors[code]}', flush=True)
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(load, code) for code in pool]
            for i, f in enumerate(as_completed(futures), 1):
                f.result()
                if i % 50 == 0:
                    print(f'History coverage {i}/1000; failures={len(self.errors)}', flush=True)

    def scan(self, date, account, cfg):
        self.load_pool()
        if self.errors:
            raise ValueError(f'History incomplete: {len(self.errors)} stocks failed; cannot report zero signals')
        days = self.days(end_date=date, count=42)
        if not days or days[-1] != date or len(days) < 42:
            raise ValueError('Trading calendar stale or incomplete')
        t = days[-2]
        usable = []
        omitted = []
        for code, meta in self.pool.items():
            if pd.Timestamp(meta['inclusion']).date() > t:
                omitted.append(code)
                continue
            if re.search(r'ST|退', meta['name'], re.I):
                continue
            df = self.frames[code]
            if self.listed[code] > t - dt.timedelta(days=250):
                continue
            df = df[df.time.dt.date <= t].copy()
            for field in ['open', 'close', 'high_limit', 'low_limit']:
                df[field] *= df.factor
            usable.append(df)
        if not usable:
            raise ValueError('No validated history')
        combined = pd.concat(usable, ignore_index=True)
        calendar = days[-42:-1]
        history = {}
        dn = None
        for prev, day in zip(calendar[:-1], calendar[1:]):
            a = combined[combined.time.dt.date == prev]
            b = combined[combined.time.dt.date == day]
            ice, dn = signals.ice_day(a, b)
            history[day.isoformat()] = ice
        run = 0
        for ice in reversed(list(history.values())):
            if not ice:
                break
            run += 1
        h = self.daily(INDEX)
        h = h[h.time.dt.date <= t].tail(20)
        ma = float(h.close.iloc[-1] / h.close.mean() - 1)
        ice = history[t.isoformat()]
        temperature = dict(ICE=ice, RUN=run, MA20=ma, M_DN=dn,
                           DEEP=bool(ice and (run >= 2 or ma <= -.04)), FB_OK=bool(ma < 0 and dn < .03), LB_OK=bool(dn < .03))
        flags = signals.stock_flags(combined, {s: float(self.frames[s][self.frames[s].time.dt.date == t].close.iloc[0]) for s in combined.code.unique() if not self.frames[s][self.frames[s].time.dt.date == t].empty}, t)
        candidates = signals.candidates(flags, temperature, self.names, cfg['min_day_money'])
        marks = {}
        for c in candidates:
            rows = self.frames[c['code']][self.frames[c['code']].time.dt.date == date]
            if len(rows) != 1 or rows.iloc[0].corporate_action:
                raise ValueError('Target-date daily unavailable/corporate action: ' + c['code'])
            r = rows.iloc[0]
            marks[c['code']] = {f: float(r[f]) for f in ['open', 'close', 'money', 'paused', 'high_limit', 'low_limit', 'factor']}
            marks[c['code']].update(is_st=bool(re.search('ST|退', c['name'], re.I)), name=c['name'])
        qualified = opening_queue(candidates, marks, cfg)
        return dict(date=str(date), previous_day=str(t), status='reference_only', temperature=temperature,
                    pool_size=len(self.pool), history_loaded=len(self.frames)-1, validated_stocks=len(flags), omitted=omitted,
                    candidates=candidates, opening_candidates=qualified,
                    limitations=['使用当前中证1000成分及名称快照，历史被调出成分和历史ST状态无法完整还原。',
                                 '市场温度使用上述可验证股票，非历史逐日完整成分；结果为参考口径，不能称为原聚宽口径的完整历史复现。',
                                 '历史价格按新浪复权因子对齐；当日除权股票不记交易；开盘候选不等于分钟买入信号。'])

    def prepare(self, date, account, cfg):
        report = self.scan(date, account, cfg)
        temp = report['temperature'] | {'data_provenance': report['limitations'], 'reference_only': True}
        return report['candidates'], temp

    def session(self, date, candidates, positions, cfg):
        marks = {}
        for code in sorted(set(c['code'] for c in candidates) | set(positions)):
            df = self.daily(code)
            rows = df[df.time.dt.date == date]
            if len(rows) != 1 or rows.iloc[0].corporate_action:
                raise ValueError('Daily missing or corporate action: ' + code)
            r = rows.iloc[0]
            marks[code] = {f: float(r[f]) for f in ['open', 'close', 'money', 'paused', 'high_limit', 'low_limit', 'factor']}
            name = self.names.get(code, positions.get(code, {}).get('name', ''))
            if not name:
                raise ValueError('Missing security name: ' + code)
            marks[code].update(is_st=bool(re.search('ST|退', name, re.I)), name=name)
        qualified = opening_queue(candidates, marks, cfg)
        codes = sorted(set(c['code'] for c in qualified) | {s for s in positions if not marks[s]['paused']})
        bars = {}
        for code in codes:
            path = self.cache / f'minute-{code[:6]}-{date}.json'
            if path.exists():
                data = json.loads(path.read_text())
            else:
                response = get('https://push2his.eastmoney.com/api/qt/stock/trends2/get', params={
                    'secid': ('1.' if code.endswith('XSHG') else '0.')+code[:6],
                    'ndays': 5, 'iscr': 0,
                    'fields1': 'f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11',
                    'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58'})
                payload = response.json().get('data')
                if not payload or not payload.get('trends'):
                    raise ValueError('Minute source empty: ' + code)
                data = {}
                for item in payload['trends']:
                    x = item.split(',')
                    stamp = pd.Timestamp(x[0])
                    if stamp.date() != date or stamp.strftime('%H:%M') < '09:31':
                        continue
                    tm = stamp.strftime('%H:%M')
                    if tm in data:
                        raise ValueError('Duplicate minute: ' + code)
                    data[tm] = dict(zip(['open', 'close', 'high', 'low', 'volume', 'money'], map(float, x[1:7])))
                    data[tm]['volume'] *= 100  # Eastmoney lots -> shares.
                path.write_text(json.dumps(data), encoding='utf-8')
            if code in {c['code'] for c in qualified} and any(f'09:{i:02d}' not in data for i in range(31, 37)):
                raise ValueError('Buy minutes incomplete: ' + code)
            if code in positions and any(f'14:{i:02d}' not in data for i in range(50, 58)):
                raise ValueError('Exit minutes incomplete: ' + code)
            bars[code] = data
        return marks, bars
