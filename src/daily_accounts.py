"""Independent A/B paper accounts: verified opening price in, next-session close out."""
import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from .data import ROOT, PublicData, price_limit
from .opening_push import quote_batch, make_opening_report
from .paper import DEFAULTS, positive, lot_amount, new_account, total_value, band, COMPONENTS
from .run import write_json, write_csv
from .notify import endpoint
import requests

TZ = ZoneInfo('Asia/Shanghai')


def load_accounts(root):
    path = root / 'state/daily_accounts.json'
    if path.exists():
        return json.loads(path.read_text())
    legacy = root / 'state/account.json'
    a = json.loads(legacy.read_text()) if legacy.exists() else new_account('2026-10-26')
    return dict(schema=2, model='opening_price_next_session_close',
                migration='A preserves legacy cash, holdings and trades; old fills are not rewritten. B starts independently.',
                A=a, B=new_account('2026-10-26'), sessions={}, notifications={})


def buy(account, candidates, quotes, day, cfg, gap_required):
    fills, rejected = [], []
    for c in candidates:
        code = c['code']
        if len(account['positions']) >= cfg['slots'] or len(fills) >= cfg['max_new']:
            break
        if code in account['positions']:
            continue
        q = quotes.get(code)
        if not q or not positive(q.get('open')) or not positive(q.get('prev_close')):
            raise ValueError('Missing opening quote: ' + code)
        price, prev = q['open'], q['prev_close']
        if gap_required:
            lo, hi = COMPONENTS[c['component']]['gap']
            if not lo-1e-9 <= price/prev-1 <= hi+1e-9:
                rejected.append(dict(code=code, reason='opening gap outside component range'))
                continue
        low, high = price_limit(prev, -band(code)), price_limit(prev, band(code))
        if ('ST' in q['name'].upper() or '退' in q['name'] or
                price <= low or price >= high or price > cfg['max_price']):
            rejected.append(dict(code=code, reason='ST/limit/price restriction'))
            continue
        budget = min(total_value(account) / cfg['slots'], account['cash'] * (1-cfg['cash_buffer']),
                     c['money'] * cfg['day_participation'])
        amount = lot_amount((budget-max(cfg['min_commission'], budget*cfg['commission'])) / price, code)
        if amount <= 0:
            rejected.append(dict(code=code, reason='insufficient budget'))
            continue
        gross = round(amount*price, 2)
        fee = round(max(cfg['min_commission'], gross*cfg['commission']), 2)
        account['cash'] = round(account['cash']-gross-fee, 2)
        account['positions'][code] = dict(name=q['name'], component=c['component'], amount=amount,
            entry_date=day, cost=gross+fee, mark=price)
        fill = dict(date=day, time='09:30', side='buy', code=code, name=q['name'], amount=amount,
                    price=price, commission=fee, tax=0, model='verified_open_price_assumption')
        account['trades'].append(fill)
        fills.append(fill)
    return fills, rejected


def close_account(account, day, daily, cfg):
    fills, blocked = [], []
    for code, p in list(account['positions'].items()):
        d = daily[code]
        if not positive(d.get('close')):
            raise ValueError('Missing daily close: '+code)
        p['mark'] = d['close']
        if p['entry_date'] >= day:
            continue
        if d['paused'] or d['close'] <= d['low_limit'] + 1e-8:
            blocked.append(dict(code=code, reason='suspended or close at lower limit'))
            continue
        gross = round(p['amount'] * d['close'], 2)
        fee = round(max(cfg['min_commission'], gross*cfg['commission']), 2)
        tax = round(gross*cfg['sell_tax'], 2)
        fill = dict(date=day, time='15:00', side='sell', code=code, name=p['name'], amount=p['amount'],
                    price=d['close'], commission=fee, tax=tax,
                    realised=round(gross-fee-tax-p['cost'], 2), model='daily_close_price_assumption')
        account['cash'] = round(account['cash']+gross-fee-tax, 2)
        account['trades'].append(fill)
        fills.append(fill)
        del account['positions'][code]
    value = round(total_value(account), 2)
    account['last_processed'] = day
    account['equity'].append(dict(date=day, equity=value, cash=account['cash'],
                                  return_pct=round((value/account['initial_cash']-1)*100, 4)))
    return fills, blocked


def save(root, state):
    write_json(root / 'state/daily_accounts.json', state)
    for name in ('A', 'B'):
        account = state[name]
        write_csv(root/f'logs/trades-{name}.csv', account['trades'],
                  ['date','time','code','name','side','amount','price','commission','tax','realised','model'])
        write_csv(root/f'logs/equity-{name}.csv', account['equity'], ['date','cash','equity','return_pct'])
        write_csv(root/f'outputs/holdings-{name}.csv', [dict(code=c, **p) for c,p in account['positions'].items()],
                  ['code','name','entry_date','amount','cost','mark','component'])
    lines = ['# S18 A/B独立模拟账户', '', '按开盘价模拟买入，买入后的下一交易日按收盘价模拟卖出。停牌或收盘跌停顺延。',
             '成交是模拟价格假设；A仓沿用旧账本，历史缺失卖出未被补造成交。', '']
    for name in ('A', 'B'):
        a = state[name]
        lines += [f'## {name}仓', '', f"现金：{a['cash']:.2f}；权益：{total_value(a):.2f}；日终更新：{a['last_processed']}", '',
                  '|股票|名称|买入日|股数|最新价|', '|---|---|---|---:|---:|']
        lines += [f"|{code}|{p['name']}|{p['entry_date']}|{p['amount']}|{p['mark']:.2f}|" for code,p in a['positions'].items()]
        lines += ['']
    (root/'outputs').mkdir(exist_ok=True)
    (root/'outputs/latest.md').write_text('\n'.join(lines)+'\n')
    write_json(root/'outputs/latest.json', {n: dict(cash=state[n]['cash'], equity=total_value(state[n]),
        positions=state[n]['positions'], last_processed=state[n]['last_processed']) for n in ('A','B')})


def notify(root, state, key_id, report):
    if state['notifications'].get(key_id):
        return
    key = next((os.getenv(k) for k in ('SERVERCHAN_SENDKEY','SERVERCHAN_KEY','SERVER_CHAN_SENDKEY','SCKEY') if os.getenv(k)), '')
    if not key:
        raise RuntimeError('模拟成交已保存，但未配置成交推送密钥；重跑仅重试推送')
    lines = ['模拟成交已记账（非券商成交）']
    for name in ('A','B'):
        rows = report[name]['fills']
        lines += [f'{name}仓：'+('无成交' if not rows else '')]
        lines += [f"{f['side']} {f['code']} {f['name']} {f['amount']}股 @ {f['price']:.2f}" for f in rows]
        lines += [f"未执行：{x['code']} {x['reason']}" for x in report[name].get('blocked', [])]
    try:
        r = requests.post(endpoint(key), data=dict(title=f'S18 {key_id} 模拟成交确认', desp='\n\n'.join(lines)), timeout=15)
        r.raise_for_status()
        if r.json().get('code') != 0:
            raise ValueError()
    except (requests.RequestException, ValueError):
        raise RuntimeError('模拟成交已保存，Server酱推送失败；重跑仅重试推送') from None
    state['notifications'][key_id] = True
    save(root, state)


def close_quotes(codes, now, fetch=requests.get):
    """Capture final same-session Tencent quotes; never reconstruct old fills."""
    import re
    result = {}
    for start in range(0, len(codes), 50):
        batch = codes[start:start+50]
        symbols = [('sh' if c.startswith('6') else 'sz') + c[:6] for c in batch]
        response = fetch('https://qt.gtimg.cn/q=' + ','.join(symbols),
                         timeout=12, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        for symbol, raw in re.findall(r'v_(s[hz]\d{6})="([^"]*)"', response.content.decode('gbk', errors='replace')):
            code = next((c for c in batch if c[:6] == symbol[2:]), None)
            fields = raw.split('~')
            if code is None or len(fields) < 49 or fields[2] != symbol[2:]:
                continue
            try:
                stamp = dt.datetime.strptime(fields[30], '%Y%m%d%H%M%S').replace(tzinfo=TZ)
                price, lower, volume = float(fields[3]), float(fields[48]), float(fields[6])
                if (stamp.date() != now.date() or stamp.hour != 15 or
                        not 0 <= (now-stamp).total_seconds() <= 900 or
                        not positive(price) or not positive(lower) or volume < 0):
                    continue
                result[code] = dict(close=price, low_limit=lower, paused=volume == 0,
                                    quote_time=stamp.isoformat(), source='tencent_final_quote')
            except (ValueError, IndexError):
                continue
    if set(result) != set(codes):
        raise ValueError('Final close quote missing/unconfirmed: ' + ','.join(sorted(set(codes)-set(result))))
    return result


def execute(mode, root=ROOT, now=None):
    root = Path(root)
    now = now or dt.datetime.now(TZ)
    day = now.date().isoformat()
    state = load_accounts(root)
    key_id = day+'-'+('close' if mode == 'capture-close' else mode)
    if key_id in state['sessions']:
        notify(root, state, key_id, state['sessions'][key_id])
        return
    # Opening daily index bars may not exist at 09:30; use an exchange calendar.
    import akshare as ak
    calendar = [dt.date.fromisoformat(str(x)[:10]) for x in ak.tool_trade_date_hist_sina()['trade_date']]
    if not calendar or max(calendar) < now.date():
        raise ValueError('Exchange calendar does not cover today')
    if now.date() not in calendar:
        print('Non-trading day; no trades')
        return
    cfg = DEFAULTS | json.loads((root/'config.json').read_text())
    report = {}
    working = copy.deepcopy(state)
    if mode == 'open':
        if not (now.hour == 9 and 30 <= now.minute <= 35):
            raise ValueError('Opening execution requires 09:30-09:35; no late backfill')
        previous_day = max(x for x in calendar if x < now.date()).isoformat()
        previous = json.loads((root/f'outputs/signals-{previous_day}.json').read_text())
        a_candidates = previous['next_session']['candidates']
        b_candidates = previous['opening_candidates'][:cfg['max_new']]
        codes = sorted({c['code'] for c in a_candidates+b_candidates})
        quotes = quote_batch(codes, now.date())
        for code in codes:
            q = quotes.get(code)
            if not q:
                raise ValueError('Opening quote missing: '+code)
            stamp = dt.datetime.strptime(q['quote_time'], '%Y%m%d%H%M%S').replace(tzinfo=TZ)
            if abs((now-stamp).total_seconds()) > 90:
                raise ValueError('Opening quote stale: '+code)
        write_json(root/f'outputs/evidence-{day}-open.json', dict(captured_at=now.isoformat(), quotes=quotes,
                   signal_date=previous_day, A_candidates=a_candidates, B_candidates=b_candidates))
        selected = make_opening_report(previous, now.date(), quotes, checked_at=now, strict_data=True)
        for name, candidates in [('A', selected), ('B', b_candidates)]:
            fills, blocked = buy(working[name], candidates, quotes, day, cfg, name=='A')
            report[name] = dict(fills=fills, blocked=blocked)
    elif mode == 'capture-close':
        if not (now.hour == 15 and now.minute <= 10):
            raise ValueError('Close capture requires 15:00-15:10; no historical backfill')
        codes = sorted({code for name in ('A','B') for code in working[name]['positions']})
        daily = close_quotes(codes, now)
        write_json(root/f'outputs/evidence-{day}-close.json',
                   dict(captured_at=now.isoformat(), quotes=daily, model='final_quote_capture'))
        for name in ('A','B'):
            fills, blocked = close_account(working[name], day, daily, cfg)
            for fill in fills:
                fill['model'] = 'captured_final_close_assumption'
            report[name] = dict(fills=fills, blocked=blocked, status='captured_at_close')
    else:
        # Evening reconciliation reads the saved ledger only. Never fetch old
        # minute/daily prices to invent a missed intraday execution.
        if now.hour < 16:
            print('Close reconciliation waits until 16:00; no execution attempted')
            return
        for name in ('A','B'):
            blocked = [dict(code=code, reason='MISSED_CLOSE_CAPTURE: no captured final quote; position retained')
                       for code in working[name]['positions']]
            report[name] = dict(fills=[], blocked=blocked, status='missed_close_capture')
        working['sessions'][key_id] = report
        save(root, working)
        write_json(root/f'outputs/execution-{key_id}.json', report)
        print('MISSED_CLOSE_CAPTURE: ledger retained; no historical fill replay')
        return
    working['sessions'][key_id] = report
    save(root, working)
    write_json(root/f'outputs/execution-{key_id}.json', report)
    notify(root, working, key_id, report)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['open','close','capture-close'])
    execute(p.parse_args().mode)

if __name__ == '__main__':
    main()
