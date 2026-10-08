"""Deterministic, post-close paper execution. No broker orders are sent."""
import copy
import math
from decimal import Decimal, ROUND_FLOOR

DEFAULTS = dict(initial_cash=200000, slots=2, max_new=2, max_price=190,
                min_day_money=1e8, day_participation=.001,
                minute_participation=.10, cash_buffer=.01, max_chase=.003,
                near_down=.005, commission=.00025, min_commission=5,
                sell_tax=.0005, slippage=.001, start_date='2026-10-08')
COMPONENTS = {
    'FB_dip': dict(day='FB_OK', stock='fb', gap=(-.07, -.02), pri=0),
    'LB': dict(day='LB_OK', stock='lb', gap=(-.04, -.02), pri=1),
    'DEEP': dict(day='DEEP', stock='gd', gap=(-.09, -.03), pri=2),
}


def new_account(start_date):
    return dict(schema=1, initial_cash=200000., cash=200000., start_date=start_date,
                last_processed=None, positions={}, trades=[], equity=[], days=[], ice_hist={})


def positive(x):
    try:
        return math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


def floor_cent(x):
    return float(Decimal(str(x)).quantize(Decimal('.01'), rounding=ROUND_FLOOR))


def band(code):
    return .20 if code.startswith(('300', '301', '688', '689')) else .10


def lot_amount(shares, code):
    shares = max(0, int(shares))
    if code.startswith(('688', '689')):
        return shares if shares >= 200 else 0
    return shares // 100 * 100


def total_value(account):
    return account['cash'] + sum(p['amount'] * p['mark'] for p in account['positions'].values())


def make_buy(account, c, last, previous_money, tm, cfg):
    if not positive(last) or not positive(previous_money):
        return None
    lo, hi = COMPONENTS[c['component']]['gap']
    if not lo - 1e-9 <= last / c['prev_close'] - 1 <= hi + 1e-9:
        return None
    if last <= c['low_limit'] * (1 + cfg['near_down']) or last >= c['high_limit']:
        return None
    cap = min(c['open'] * (1 + cfg['max_chase']), c['prev_close'] * (1 + hi), cfg['max_price'])
    limit = floor_cent(min(last * 1.001, cap, c['high_limit'] - .01))
    if limit + 1e-8 < last:
        return None
    budget = min(total_value(account) / cfg['slots'], account['cash'] * (1 - cfg['cash_buffer']),
                 c['money'] * cfg['day_participation'], previous_money * cfg['minute_participation'])
    amount = lot_amount((budget - max(cfg['min_commission'], budget * cfg['commission'])) / limit, c['code'])
    if amount == 0:
        return None
    return dict(code=c['code'], name=c['name'], side='buy', amount=amount, limit=limit,
                low_limit=c['low_limit'], high_limit=c['high_limit'],
                submitted=tm, component=c['component'])


def match(account, order, bar, date, tm, cfg):
    """Match only the subsequent minute; touching a limit alone is insufficient."""
    if tm <= order['submitted'] or not all(positive(bar.get(x)) for x in ('open', 'low', 'high', 'money', 'volume')):
        return None
    if bar['low'] > bar['high'] or not bar['low'] <= bar['open'] <= bar['high']:
        raise ValueError('Invalid OHLC bar')
    side, code, limit = order['side'], order['code'], order['limit']
    if side == 'buy':
        if bar['low'] >= limit or bar['low'] >= order['high_limit'] - 1e-8:
            return None
        # Conservative fill at the submitted limit, including slippage within that limit.
        price = limit
        capacity = min(order['amount'], bar['volume'] * cfg['minute_participation'],
                       bar['money'] * cfg['minute_participation'] / price,
                       (account['cash'] - cfg['min_commission']) / (price * (1 + cfg['commission'])))
        amount = lot_amount(capacity, code)
    else:
        p = account['positions'].get(code)
        if not p or p['entry_date'] >= date:
            return None
        if bar['high'] <= limit or bar['high'] <= order['low_limit'] + 1e-8:
            return None
        price = max(limit, floor_cent(bar['open'] * (1 - cfg['slippage'])))
        price = min(bar['high'], max(bar['low'], price))
        capacity = int(min(order['amount'], p['amount'], bar['volume'] * cfg['minute_participation'],
                           bar['money'] * cfg['minute_participation'] / price))
        amount = capacity if capacity >= p['amount'] else lot_amount(capacity, code)
    if amount <= 0:
        return None
    gross = round(price * amount, 2)
    commission = round(max(cfg['min_commission'], gross * cfg['commission']), 2)
    tax = round(gross * cfg['sell_tax'], 2) if side == 'sell' else 0.
    realised = 0.
    if side == 'buy':
        if code in account['positions']:
            raise ValueError('No topping up an existing position')
        account['cash'] = round(account['cash'] - gross - commission, 2)
        account['positions'][code] = dict(amount=amount, entry_date=date, cost=gross + commission,
                                        mark=price, name=order['name'], component=order['component'])
    else:
        p = account['positions'][code]
        allocated_cost = p['cost'] * amount / p['amount']
        realised = round(gross - commission - tax - allocated_cost, 2)
        account['cash'] = round(account['cash'] + gross - commission - tax, 2)
        p['cost'] = round(p['cost'] - allocated_cost, 2)
        p['amount'] -= amount
        if p['amount'] == 0:
            del account['positions'][code]
    trade = dict(date=date, time=tm, code=code, side=side, amount=amount, price=price,
                 gross=gross, commission=commission, tax=tax, realised=realised,
                 component=order['component'], model='next_minute_limit_assumption')
    account['trades'].append(trade)
    return trade


def opening_queue(candidates, daily, cfg):
    out = []
    for c in candidates:
        d = daily.get(c['code'])
        if not d:
            raise ValueError('Missing opening data: ' + c['code'])
        if d['paused'] or d['is_st'] or 'ST' in d['name'] or '退' in d['name']:
            continue
        if not all(positive(d[x]) for x in ('open', 'high_limit', 'low_limit')):
            continue
        if d['open'] > cfg['max_price'] or d['open'] >= d['high_limit']:
            continue
        if abs(d['high_limit'] - c['prev_close'] * (1 + band(c['code']))) > .021:
            continue
        gap = d['open'] / c['prev_close'] - 1
        lo, hi = COMPONENTS[c['component']]['gap']
        if lo - 1e-9 <= gap <= hi + 1e-9 and d['open'] > d['low_limit'] * (1 + cfg['near_down']):
            out.append(dict(c, open=d['open'], gap=gap, high_limit=d['high_limit'], low_limit=d['low_limit']))
    return sorted(out, key=lambda x: (x['pri'], x['gap'], x['code']))


def replay_day(account, date, candidates, daily, minutes, cfg, preparation_error=None):
    if date < account['start_date']:
        raise ValueError('Date precedes paper account start')
    if account['last_processed'] and date <= account['last_processed']:
        return False
    # Preflight before any mutation. Suspended stocks do not require minute bars.
    for code, d in daily.items():
        if not positive(d.get('close')):
            raise ValueError('Invalid end-of-day mark: ' + code)
    for code, rows in minutes.items():
        for tm, bar in rows.items():
            if not all(positive(bar.get(f)) for f in ('open', 'high', 'low', 'close')):
                raise ValueError('Invalid minute OHLC: ' + code + ' ' + tm)
            if not all(math.isfinite(bar.get(f, float('nan'))) and bar[f] >= 0 for f in ('money', 'volume')):
                raise ValueError('Invalid minute liquidity: ' + code + ' ' + tm)
            if not (bar['low'] <= bar['open'] <= bar['high'] and bar['low'] <= bar['close'] <= bar['high']):
                raise ValueError('Inconsistent minute OHLC: ' + code + ' ' + tm)
    for code, p in account['positions'].items():
        d = daily.get(code)
        if not d or not positive(d.get('close')):
            raise ValueError('Missing held-stock daily mark: ' + code)
        if not d['paused']:
            required = [f'14:{i:02d}' for i in range(50, 58)]
            if any(t not in minutes.get(code, {}) for t in required):
                raise ValueError('Missing held-stock exit minutes: ' + code)
    queue = opening_queue(candidates, daily, cfg) if not preparation_error else []
    for c in queue:
        if any(f'09:{i:02d}' not in minutes.get(c['code'], {}) for i in range(31, 37)):
            raise ValueError('Missing candidate buy minutes: ' + c['code'])
    a = copy.deepcopy(account)
    pending, selected, filled, attempts, events = {}, set(), set(), {}, []
    times = [f'09:{i:02d}' for i in range(31, 37)] + [f'14:{i:02d}' for i in range(50, 58)]
    for tm in times:
        # Decisions below use only the close of the just-completed minute.
        for code, p in a['positions'].items():
            bar = minutes.get(code, {}).get(tm)
            if bar and positive(bar.get('close')):
                p['mark'] = bar['close']
        for code, order in list(pending.items()):
            bar = minutes.get(code, {}).get(tm)
            trade = match(a, order, bar, date, tm, cfg) if bar else None
            if trade:
                if order['side'] == 'buy':
                    filled.add(code)
                events.append(dict(time=tm, event='FILL', code=code, amount=trade['amount']))
                del pending[code]  # Partial buy remainder is canceled, never topped up.
            elif order['side'] == 'buy' or order['limit'] > order['low_limit'] + 1e-8:
                events.append(dict(time=tm, event='EXPIRE', code=code))
                del pending[code]
        if '09:31' <= tm <= '09:35':
            prior_tm = f'09:{int(tm[-2:]) - 1:02d}'
            for c in queue:
                code = c['code']
                if len(set(a['positions']) | set(pending)) >= cfg['slots']:
                    break
                if code in a['positions'] or code in pending or code in filled:
                    continue
                if code not in selected and len(selected) >= cfg['max_new']:
                    continue
                if attempts.get(('buy', code), 0) >= 2:
                    continue
                bar = minutes[code].get(tm)
                prior = minutes[code].get(prior_tm)
                if not bar or not prior:
                    continue
                order = make_buy(a, c, bar['close'], prior['money'], tm, cfg)
                if order:
                    selected.add(code)
                    attempts['buy', code] = attempts.get(('buy', code), 0) + 1
                    pending[code] = order
                    events.append(dict(time=tm, event='SUBMIT', **order))
        if '14:50' <= tm <= '14:56':
            for code, p in list(a['positions'].items()):
                if p['entry_date'] >= date or code in pending or daily[code]['paused']:
                    continue
                if attempts.get(('sell', code), 0) >= 5:
                    continue
                bar = minutes.get(code, {}).get(tm)
                if not bar or not positive(bar['close']):
                    continue
                d = daily[code]
                order = dict(code=code, side='sell', amount=p['amount'],
                             limit=max(d['low_limit'], floor_cent(bar['close'] * .995)),
                             high_limit=d['high_limit'], low_limit=d['low_limit'], submitted=tm,
                             component=p.get('component', 'unknown'))
                pending[code] = order
                attempts['sell', code] = attempts.get(('sell', code), 0) + 1
                events.append(dict(time=tm, event='SUBMIT', **order))
    for code, p in a['positions'].items():
        p['mark'] = daily[code]['close']
    equity = round(total_value(a), 2)
    a['equity'].append(dict(date=date, cash=a['cash'], market_value=round(equity - a['cash'], 2),
                            equity=equity, return_pct=round((equity / a['initial_cash'] - 1) * 100, 4)))
    a['days'].append(dict(date=date, candidates=candidates, queue=queue, events=events,
                          preparation_error=preparation_error,
                          unfilled_at_close=list(pending.values())))
    a['last_processed'] = date
    if a['cash'] < 0 or len(a['positions']) > cfg['slots']:
        raise ValueError('Account invariant violated')
    account.clear()
    account.update(a)
    return True
