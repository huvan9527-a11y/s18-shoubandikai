"""Daily-only reference event study. Never reads or writes the paper ledger."""
import datetime as dt
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import numpy as np
import pandas as pd
from src.data import PublicData, INDEX, get, symbol, code_id, price_limit
from src.paper import DEFAULTS, band, opening_queue
from src import signals

START = dt.date(2025, 10, 1)
END = dt.date(2026, 10, 9)
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'outputs/comparison-20251001'
OUT.mkdir(parents=True, exist_ok=True)

class History(PublicData):
    def __init__(self):
        super().__init__()
        self.cache = ROOT / 'cache/research'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.listed = {}

    def daily(self, code):
        if code in self.frames:
            return self.frames[code]
        path = self.cache / f'{symbol(code)}-{END}.json'
        if path.exists():
            payload = json.loads(path.read_text())
        elif code == INDEX:
            rows = []
            for year in (2025, 2026):
                data = get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get', params={
                    'param': f'sh000852,day,{year}-01-01,{year}-12-31,640,'}).json()['data']['sh000852']
                rows.extend(dict(date=x[0], open=x[1], close=x[2], high=x[3], low=x[4], volume=x[5], amount=0)
                            for x in data['day'])
            payload = dict(rows=rows, factors=[])
        else:
            from akshare.stock.cons import zh_sina_a_stock_hist_url, hk_js_decode, zh_sina_a_stock_qfq_url
            from py_mini_racer import MiniRacer
            response = get(zh_sina_a_stock_hist_url.format(symbol(code)))
            js = MiniRacer(); js.eval(hk_js_decode)
            rows = js.call('d', response.text.split('=', 1)[1].split(';')[0].replace('"', ''))
            factor_text = get(zh_sina_a_stock_qfq_url.format(symbol(code))).text
            factors = json.JSONDecoder().raw_decode(factor_text.split('=', 1)[1].lstrip())[0]['data']
            payload = dict(rows=rows, factors=factors, listed_date=str(rows[0]['date'])[:10])
        if not payload['rows']:
            raise ValueError('Empty history ' + code)
        payload['rows'] = [x for x in payload['rows'] if '2025-01-01' <= str(x['date'])[:10] <= str(END)]
        path.write_text(json.dumps(payload))
        df = pd.DataFrame(payload['rows'])
        df['time'] = pd.to_datetime(df['date'], utc=True).dt.tz_localize(None).dt.normalize()
        df = df.sort_values('time').drop_duplicates('time').reset_index(drop=True)
        for f in ('open', 'high', 'low', 'close', 'volume', 'amount'):
            df[f] = pd.to_numeric(df[f], errors='raise')
        if df[['open','high','low','close','volume','amount']].isna().any().any():
            raise ValueError('Incomplete OHLC ' + code)
        self.listed[code] = pd.Timestamp(payload.get('listed_date', '1900-01-01')).date()
        df['money'] = df.amount; df['paused'] = (df.volume <= 0).astype(int); df['code'] = code
        df['factor'] = 1.
        for f in sorted(payload['factors'], key=lambda x: x['d'], reverse=True):
            df.loc[df.time < pd.Timestamp(f['d']), 'factor'] = 1 / float(f['f'])
        prev = df.close.shift(1) * df.factor.shift(1) / df.factor
        df['high_limit'] = prev.map(lambda p: price_limit(p, band(code)) if pd.notna(p) else np.nan)
        df['low_limit'] = prev.map(lambda p: price_limit(p, -band(code)) if pd.notna(p) else np.nan)
        df['corporate_action'] = df.factor.diff().abs().fillna(0) > 1e-10
        df['d'] = df.time.dt.date
        self.frames[code] = df
        return df

def snapshots():
    """Archived monthly files used only from FOLLOWING month to avoid timestamp lookahead."""
    result = {}
    months = pd.date_range('2025-04-01', '2026-09-01', freq='MS')
    def load(month):
        path = ROOT / 'cache/research' / f'pool-{month:%Y%m}.json'
        if path.exists():
            rows = json.loads(path.read_text())
        else:
            url = f'https://raw.githubusercontent.com/yfiua/index-constituents/main/docs/{month:%Y/%m}/constituents-csi1000.json'
            rows = get(url).json(); path.write_text(json.dumps(rows, ensure_ascii=False))
        pool = {code_id(x['Symbol'][:6]): x['Name'] for x in rows}
        if len(pool) != 1000: raise ValueError(f'Incomplete snapshot {month}: {len(pool)}')
        effective = (month + pd.offsets.MonthBegin(1)).date()
        return effective, pool
    with ThreadPoolExecutor(max_workers=8) as ex:
        for effective, pool in ex.map(load, months):
            result[effective] = pool
    return result

def portfolio(events, raw, days, buy, offset, sell):
    cash=200000.; positions={}; equity=[]; fills=[]; skips=[]
    dayevents={}
    for e in events: dayevents.setdefault(e['date'],[]).append(e)
    cfg=DEFAULTS
    for k,d in enumerate(days):
        if d<START: continue
        bought=0
        for endpoint in ('open','close'):
            if endpoint==sell:
                for c,p in list(positions.items()):
                    if k<p['due'] or d not in raw[c].index:continue
                    r=raw[c].loc[d]
                    if r.paused or r[sell]<=r.low_limit+1e-6:continue
                    price=max(float(r.low_limit),float(r[sell])*(1-cfg['slippage']))
                    effective_units=p['units']*float(r.factor)/p['factor']
                    gross=price*effective_units
                    proceeds=gross-max(5.,gross*cfg['commission'])-gross*cfg['sell_tax']
                    cash+=proceeds
                    fills.append(dict(code=c,entry_date=p['date'],exit_date=str(d),units=p['units'],
                                      cost=p['cost'],proceeds=proceeds,profit=proceeds-p['cost'],component=p['component']))
                    del positions[c]
            if endpoint==buy:
                for e in sorted(dayevents.get(str(d),[]),key=lambda x:(x['pri'],x['gap'],x['code'])):
                    c=e['code'];r=raw[c].loc[d]
                    if c in positions or len(positions)>=2 or bought>=2:
                        skips.append(dict(date=str(d),code=c,reason='slots_or_duplicate'));continue
                    if r.paused or (buy=='close' and r.close>=r.high_limit-1e-6):
                        skips.append(dict(date=str(d),code=c,reason='entry_unfilled'));continue
                    value=cash+sum(p['units']*float(raw[s].loc[:d].iloc[-1][endpoint] if d in raw[s].index else raw[s].loc[:d].iloc[-1]['close'])*
                                   float(raw[s].loc[:d].iloc[-1]['factor'])/p['factor'] for s,p in positions.items())
                    budget=min(value/2*.99,cash/1.00025,float(e['money'])*cfg['day_participation'])
                    price=min(float(r.high_limit),float(r[buy])*(1+cfg['slippage']))
                    units=int(budget/price/100)*100
                    if units< (200 if c.startswith(('688','689')) else 100):
                        skips.append(dict(date=str(d),code=c,reason='cash_or_lot_limit'));continue
                    gross=price*units; cost=gross+max(5.,gross*cfg['commission'])
                    if cost>cash+1e-6:raise ValueError('Portfolio cash exceeded')
                    cash-=cost; bought+=1
                    positions[c]=dict(units=units,factor=float(r.factor),date=str(d),cost=cost,
                                      due=k+offset,component=e['component'])
        value=cash+sum(p['units']*float(raw[s].loc[:d].iloc[-1]['close'])*
                       float(raw[s].loc[:d].iloc[-1]['factor'])/p['factor'] for s,p in positions.items())
        if cash<-.001 or len(positions)>2:raise ValueError('Portfolio constraints violated')
        equity.append(dict(date=str(d),equity=value,cash=cash,positions=len(positions)))
    values=np.array([200000.]+[x['equity'] for x in equity])
    dd=values/np.maximum.accumulate(values)-1
    return dict(scheme=f'{buy}_D{offset}_{sell}',total_return=float(values[-1]/200000-1),
                final_equity=float(values[-1]),max_drawdown=float(dd.min()),closed_trades=len(fills),
                open_positions=len(positions),win_rate=float(np.mean([x['profit']>0 for x in fills])) if fills else None,
                equity=equity,fills=fills,skips=skips)

def run():
    source = History(); pools = snapshots()
    union = sorted(set().union(*(set(x) for x in pools.values())))
    print(f'Historical monthly snapshot union: {len(union)}', flush=True)
    failures = {}
    def load(code):
        try: source.daily(code)
        except Exception as e: failures[code] = f'{type(e).__name__}: {e}'
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = [ex.submit(load, c) for c in union]
        for n, f in enumerate(as_completed(futures), 1):
            f.result()
            if n % 50 == 0: print(f'Loaded {n}/{len(union)} failures={len(failures)}', flush=True)
    (OUT / 'coverage.json').write_text(json.dumps(dict(stocks=len(union), failures=failures), ensure_ascii=False, indent=2))
    if failures: raise ValueError(f'{len(failures)} histories unavailable; refusing incomplete signal census')
    index = source.daily(INDEX).set_index('d')
    days = sorted(index.index)
    if days[-1] < END or days[0] > dt.date(2025, 6, 1): raise ValueError('Index calendar incomplete')
    raw = {c: f.set_index('d') for c, f in source.frames.items() if c != INDEX}
    combined = pd.concat([f for c, f in source.frames.items() if c != INDEX], ignore_index=True)
    for field in ('open','close','high_limit','low_limit'):
        combined[field] *= combined.factor
    byday = {d: group for d, group in combined.groupby('d')}
    def pool_at(d):
        eligible = [x for x in pools if x <= d]
        if not eligible: raise ValueError('No earlier constituent snapshot')
        return pools[max(eligible)]
    ice_history = {}; temps = {}; events = []; excluded = []; scan_days = []
    for i in range(61, len(days)):
        d, t = days[i], days[i-1]
        if t < min(pools): continue
        names = pool_at(t)
        usable = {c for c, name in names.items() if not re.search('ST|退',name,re.I)
                  and source.listed[c] <= t-dt.timedelta(days=250)}
        a = byday[days[i-2]]; b = byday[t]
        ice, dn = signals.ice_day(a[a.code.isin(usable)], b[b.code.isin(usable)])
        ice_history[t] = ice
        run_count = 0
        for v in reversed(list(ice_history.values())):
            if not v: break
            run_count += 1
        h = index.loc[:t].tail(20)
        ma = float(h.close.iloc[-1]/h.close.mean()-1)
        temp = dict(ICE=ice,RUN=run_count,MA20=ma,M_DN=dn,
                    DEEP=bool(ice and (run_count>=2 or ma<=-.04)),FB_OK=bool(ma<0 and dn<.03),LB_OK=bool(dn<.03))
        if d < START: continue
        hist = combined[combined.d.isin(days[i-61:i]) & combined.code.isin(usable)]
        prevclose = {c: float(raw[c].loc[t,'close']) for c in usable if t in raw[c].index}
        flags = signals.stock_flags(hist,prevclose,t)
        cands = signals.candidates(flags,temp,names,DEFAULTS['min_day_money'])
        marks = {}; valid = []
        for c in cands:
            code = c['code']
            if d not in raw[code].index:
                excluded.append(dict(date=str(d),code=code,reason='missing_or_suspended_signal_day')); continue
            r = raw[code].loc[d]
            if r.corporate_action:
                excluded.append(dict(date=str(d),code=code,reason='signal_day_corporate_action')); continue
            marks[code] = {f:float(r[f]) for f in ('open','close','money','paused','high_limit','low_limit')}
            marks[code].update(is_st=False,name=c['name']); valid.append(c)
        q = opening_queue(valid,marks,DEFAULTS)
        for c in q: events.append(dict(date=str(d), day_index=i, **c))
        scan_days.append(dict(date=str(d),previous_day=str(t),snapshot_valid_from=str(max(x for x in pools if x<=t)),
                              candidates=len(cands),opening_signals=len(q),validated=len(flags),temperature=temp))
        if len(scan_days)%20==0: print(f'Scanned {d}; signals={len(events)}',flush=True)
    print(f'All signals {len(events)}; trading dates {len(scan_days)}',flush=True)
    trades = []
    for event in events:
        c=event['code']; k=event['day_index']; entry=raw[c].loc[days[k]]
        for buy in ('open','close'):
            for offset,sell in ((1,'open'),(1,'close'),(2,'close')):
                row=dict(event,entry=buy,exit=sell,offset=offset,scheme=f'{buy}_D{offset}_{sell}')
                if buy=='close' and (entry.paused or entry.close>=entry.high_limit-1e-6):
                    row.update(status='entry_unfilled');trades.append(row);continue
                target=k+offset; actual=target
                while actual<len(days):
                    date=days[actual]
                    if date in raw[c].index:
                        r=raw[c].loc[date]
                        # Endpoint limit-down has no guaranteed sell liquidity; defer same endpoint.
                        if not r.paused and r[sell]>r.low_limit+1e-6:break
                    actual+=1
                row['target_exit_date']=str(days[target]) if target<len(days) else None
                if actual>=len(days):row.update(status='pending');trades.append(row);continue
                r=raw[c].loc[days[actual]]
                # Factor-adjusted price ratio is a comparable research total-return proxy.
                pbuy=float(entry[buy]); psell=float(r[sell])*float(r.factor)/float(entry.factor)
                gross=psell/pbuy-1
                net=(psell*(1-.001)*(1-.00025-.0005))/(pbuy*(1+.001)*(1+.00025))-1
                row.update(status='closed',exit_date=str(days[actual]),delayed_days=actual-target,
                           buy_price=pbuy,sell_price=float(r[sell]),factor_ratio=float(r.factor)/float(entry.factor),
                           gross_return=gross,net_return=net,
                           corporate_action_during_hold=bool(raw[c].loc[days[k]:days[actual]].corporate_action.any()))
                trades.append(row)
    df=pd.DataFrame(trades); summary=[]
    for scheme,g in df.groupby('scheme'):
        closed=g[g.status=='closed']; returns=closed.net_return
        summary.append(dict(scheme=scheme,all_signals=len(g),closed=len(closed),pending=int((g.status=='pending').sum()),
                            unfilled=int((g.status=='entry_unfilled').sum()),avg_return=float(returns.mean()),
                            median_return=float(returns.median()),win_rate=float((returns>0).mean()),
                            best=float(returns.max()),worst=float(returns.min()),delayed=int((closed.delayed_days>0).sum())))
    portfolios=[portfolio(events,raw,days,buy,offset,sell) for buy in ('open','close')
                for offset,sell in ((1,'open'),(1,'close'),(2,'close'))]
    payload=dict(period=[str(START),str(END)],signals=events,daily_scans=scan_days,exclusions=excluded,portfolios=portfolios,
                 summary=summary,trades=trades,
                 assumptions=dict(slippage_each_side=.001,commission_each_side=.00025,sell_tax=.0005,
                     entry_day='D0',third_trading_day_exit='D+2',signal_set='same opening-qualified set for both entry endpoints',
                     constituent_method='prior-month archived snapshot, lagging changes; historical daily membership not exact',
                     historical_st='monthly archived names, intramonth ST changes unavailable',
                     return_method='factor-adjusted endpoint ratio; research proxy, no minimum commission or whole-lot constraints',
                     portfolio='200000 initial cash, 2 slots, max 2 daily buys, 1% buffer, prior-day amount participation 0.1%, whole lots and min5 commission; factor-based corporate-action proxy',
                     max_drawdown='daily closing equity with initial capital; intraday drawdown unavailable'))
    (OUT/'results.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False))
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':run()
