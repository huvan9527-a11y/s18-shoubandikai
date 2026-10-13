"""09:26 CST verified Tencent auction snapshots -> ServerChan candidates, never fills."""
import datetime as dt
import json
import argparse
import re
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from .data import ROOT, code_id
from .notify import push_opening_candidates
from .paper import opening_queue, DEFAULTS

TZ = ZoneInfo("Asia/Shanghai")


def quote_batch(codes, day, fetch=requests.get):
    out = {}
    for start in range(0, len(codes), 50):
        batch = codes[start:start + 50]
        symbols = ["sh" + c[:6] if c.startswith("6") else "sz" + c[:6] for c in batch]
        resp = fetch("https://qt.gtimg.cn/q=" + ",".join(symbols),
                     timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="replace")
        for line in text.split(";"):
            m = re.search(r'v_(s[hz]\d{6})="([^"]+)"', line)
            if not m:
                continue
            parts = m.group(2).split("~")
            if len(parts) < 49:
                continue
            code = code_id(m.group(1)[2:])
            if code not in batch:
                continue
            stamp = parts[30]
            if not re.fullmatch(r"\d{14}", stamp):
                continue
            ts = dt.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=TZ)
            if ts.date() != day or (ts.hour, ts.minute) < (9, 25):
                continue
            try:
                open_px = float(parts[5])
                prev = float(parts[4])
            except (ValueError, IndexError):
                continue
            if open_px <= 0 or prev <= 0:
                continue
            out[code] = dict(open=open_px, prev_close=prev, name=parts[1], quote_time=stamp)
    return out


def evaluate_opening(yesterday, today, quote):
    """One date/price/rule path for live alerts and retrospective diagnostics."""
    from .data import price_limit
    from .paper import COMPONENTS, band, positive
    session = yesterday.get('next_session')
    if yesterday.get('status') != 'scanned' or not isinstance(session, dict):
        raise ValueError('Missing close-date next_session; regenerate daily report, never reuse prior-day candidates')
    signal_date = session.get('signal_date')
    if signal_date != yesterday.get('date') or not signal_date or dt.date.fromisoformat(signal_date) >= today:
        raise ValueError('Candidate signal date must equal report close date and precede auction date')
    candidates = session.get('candidates')
    if not isinstance(candidates, list):
        raise ValueError('Incomplete next-session candidates')
    cfg = DEFAULTS | json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    selected, rows = [], []
    for c in candidates:
        row = dict(code=c['code'], name=c['name'], component=c['component'], signal_date=signal_date,
                   signal_close=c.get('signal_close', c.get('prev_close')))
        q = quote.get(c['code'])
        try:
            expected = float(row['signal_close'])
            if c.get('signal_date', signal_date) != signal_date:
                row['status'] = 'SIGNAL_DATE_MISMATCH'
            elif not q:
                row['status'] = 'MISSING_QUOTE'
            elif not all(positive(q.get(k)) for k in ('open', 'prev_close')) or not positive(expected):
                row['status'] = 'INVALID_PRICE'
            else:
                observed = float(q['prev_close'])
                stamp = dt.datetime.strptime(q['quote_time'], '%Y%m%d%H%M%S')
                gap = float(q['open']) / observed - 1
                row.update(open=q['open'], auction_prev_close=observed, quote_time=q['quote_time'],
                           gap_pct=round(gap * 100, 3), difference_pct=round((observed / expected - 1)*100, 4))
                low, high = price_limit(observed, -band(c['code'])), price_limit(observed, band(c['code']))
                lo, hi = COMPONENTS[c['component']]['gap']
                if stamp.date() != today or (stamp.hour, stamp.minute) < (9, 25):
                    row['status'] = 'STALE_QUOTE'
                elif abs(observed / expected - 1) > .003:
                    row['status'] = 'PREV_CLOSE_MISMATCH'
                elif re.search('ST|退', q['name'], re.I):
                    row['status'] = 'ST_OR_DELIST'
                elif q['open'] > cfg['max_price']:
                    row['status'] = 'PRICE_ABOVE_MAX'
                elif q['open'] >= high:
                    row['status'] = 'AT_UPPER_LIMIT'
                elif not lo - 1e-9 <= gap <= hi + 1e-9:
                    row['status'] = 'GAP_OUTSIDE_RANGE'
                elif q['open'] <= low * (1 + cfg['near_down']):
                    row['status'] = 'TOO_CLOSE_TO_LOWER_LIMIT'
                else:
                    row['status'] = 'OPENING_RULE_MATCH'
                    selected.append(dict(c, open=q['open'], gap=gap, auction_prev_close=observed,
                                         high_limit=high, low_limit=low))
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            row.update(status='INVALID_CANDIDATE', error=str(exc))
        rows.append(row)
    selected.sort(key=lambda c: (c['pri'], c['gap'], c['code']))
    return selected, rows


def make_opening_report(yesterday, today, quote):
    selected, rows = evaluate_opening(yesterday, today, quote)
    rejected = [r for r in rows if r['status'] in ('PREV_CLOSE_MISMATCH', 'INVALID_PRICE',
                'INVALID_CANDIDATE', 'SIGNAL_DATE_MISMATCH', 'STALE_QUOTE', 'MISSING_QUOTE')]
    if rejected:
        print('AUCTION_REJECTED ' + json.dumps(rejected, ensure_ascii=False), flush=True)
    if rows and len(rejected) == len(rows):
        raise ValueError('All auction candidates rejected for data quality; not zero signals')
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--late-diagnostic', action='store_true', help='Manual post-window report; no ServerChan or trading signals')
    args = parser.parse_args(argv)
    now = dt.datetime.now(TZ)
    if now.weekday() >= 5:
        raise ValueError('Trading weekdays only')
    if not args.late_diagnostic and (now.hour != 9 or not 25 <= now.minute <= 35):
        raise ValueError('09:26 alert must run in live morning window')
    files = sorted((ROOT / "outputs").glob("signals-????-??-??.json"), reverse=True)
    previous = None
    for path in files:
        day = dt.date.fromisoformat(path.stem.removeprefix("signals-"))
        if day < now.date():
            previous = json.loads(path.read_text(encoding="utf-8"))
            break
    if not previous:
        raise ValueError("No prior-session signal report; refuse stale/empty actionable alert")
    age = (now.date() - dt.date.fromisoformat(previous["date"])).days
    if age > 4:
        raise ValueError("Previous signal report too old")
    session = previous.get('next_session')
    if not isinstance(session, dict):
        raise ValueError('Legacy report has prior-day candidates; regenerate with src.scan before opening review')
    candidates = session.get('candidates', [])
    quotes = quote_batch([c["code"] for c in candidates], now.date())
    if args.late_diagnostic:
        selected, rows = evaluate_opening(previous, now.date(), quotes)
        matched = [x for x in rows if x['status'] == 'OPENING_RULE_MATCH']
        rank = {x['code']: i for i, x in enumerate(selected)}
        matched.sort(key=lambda x: rank[x['code']])
        diagnostics = {'mode': 'RETROSPECTIVE_OPEN_REVIEW_NOT_LIVE_SIGNAL',
                       'date': now.date().isoformat(), 'checked_at': now.isoformat(),
                       'previous_signal_date': session['signal_date'],
                       'data_quality': {'signal_price_source': session['price_source'],
                                        'auction_price_source': 'tencent',
                                        'verified': all(r['status'] not in ('PREV_CLOSE_MISMATCH', 'INVALID_PRICE', 'INVALID_CANDIDATE', 'MISSING_QUOTE', 'STALE_QUOTE', 'SIGNAL_DATE_MISMATCH') for r in rows)}, 'candidates': len(candidates),
                       'fresh_quote_count': len(quotes), 'opening_rule_matches': len(matched),
                       'account_A_retrospective_top2': matched[:2],
                       'account_B_prior_opening_candidates': previous.get('opening_candidates', [])[:2],
                       'stocks': rows,
                       'warning': 'Uses current quote reported open, not archived 09:25 snapshot; no alerts, orders or fills'}
        from .run import write_json
        write_json(ROOT / ('outputs/opening-review-' + now.date().isoformat() + '.json'), diagnostics)
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2), flush=True)
        return
    selected = make_opening_report(previous, now.date(), quotes)
    # B's T+1 queue uses yesterday's already confirmed opening signals, without a new low-gap filter.
    b = previous.get("opening_candidates", [])[:2]
    if b:
        b_quotes = quote_batch([c["code"] for c in b], now.date())
        if len(b_quotes) != len(b):
            raise ValueError("Missing B-account live opening quote; do not imply tradable")
    combined = [dict(x, account="A") for x in selected[:2]]
    combined += [dict(x, account="B", note="T+1 opening plan, not a fill") for x in b]
    if not push_opening_candidates(ROOT, now.date().isoformat(), combined, data_verified=True):
        raise RuntimeError("ServerChan notification failed or not configured")
    print("Verified opening candidate alert accepted; no fills recorded")


if __name__ == "__main__":
    main()
