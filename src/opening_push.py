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


def make_opening_report(yesterday, today, quote):
    candidates = yesterday.get("candidates")
    if yesterday.get("status") != "scanned" or not isinstance(candidates, list):
        raise ValueError("Yesterday's validated candidate report is required")
    cfg = DEFAULTS | json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    from .data import price_limit
    marks = {}
    rejected = []
    for c in candidates:
        q = quote.get(c["code"])
        if not q:
            rejected.append({"code": c["code"], "reason": "missing_fresh_auction_quote"})
            continue
        expected = float(c["prev_close"])
        observed = float(q["prev_close"])
        if expected <= 0 or abs(observed / expected - 1) > .003:
            rejected.append({"code": c["code"], "reason": "previous_close_mismatch",
                             "signal_prev_close": expected, "auction_prev_close": observed,
                             "difference_pct": round((observed / expected - 1) * 100, 4) if expected > 0 else None})
            continue
        from .paper import band
        rate = band(c["code"])
        marks[c["code"]] = dict(open=q["open"], high_limit=price_limit(q["prev_close"], rate),
                                low_limit=price_limit(q["prev_close"], -rate),
                                paused=False, is_st=bool(re.search("ST|退", q["name"], re.I)),
                                name=q["name"])
    if rejected:
        print("AUCTION_REJECTED " + json.dumps(rejected, ensure_ascii=False), flush=True)
    verified = [c for c in candidates if c["code"] in marks]
    if candidates and not verified:
        raise ValueError("All auction candidates rejected; see AUCTION_REJECTED diagnostics")
    return opening_queue(verified, marks, cfg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--late-diagnostic', action='store_true', help='Manual post-window report; no ServerChan or trading signals')
    args = parser.parse_args()
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
    candidates = previous.get("candidates", [])
    quotes = quote_batch([c["code"] for c in candidates], now.date())
    if args.late_diagnostic:
        from .paper import COMPONENTS, band
        from .data import price_limit
        rows = []
        for item in candidates:
            q = quotes.get(item['code'])
            row = {'code': item['code'], 'name': item['name'], 'component': item['component'],
                   'signal_prev_close': item['prev_close']}
            if not q:
                row['status'] = 'MISSING_QUOTE'
            else:
                expected, observed = float(item['prev_close']), float(q['prev_close'])
                gap = q['open'] / observed - 1 if observed > 0 else None
                row.update(open=q['open'], auction_prev_close=observed,
                           quote_time=q['quote_time'], gap_pct=round(gap * 100, 3) if gap is not None else None)
                if expected <= 0 or observed <= 0 or abs(observed / expected - 1) > .003:
                    row['status'] = 'PREV_CLOSE_MISMATCH'
                elif re.search('ST|退', q['name'], re.I):
                    row['status'] = 'ST_OR_DELIST'
                else:
                    cfg_local = DEFAULTS | json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
                    lo, hi = COMPONENTS[item['component']]['gap']
                    low_limit = price_limit(observed, -band(item['code']))
                    high_limit = price_limit(observed, band(item['code']))
                    if q['open'] > cfg_local['max_price']:
                        row['status'] = 'PRICE_ABOVE_MAX'
                    elif q['open'] >= high_limit:
                        row['status'] = 'AT_UPPER_LIMIT'
                    elif not lo - 1e-9 <= gap <= hi + 1e-9:
                        row['status'] = 'GAP_OUTSIDE_RANGE'
                    elif q['open'] <= low_limit * (1 + cfg_local['near_down']):
                        row['status'] = 'TOO_CLOSE_TO_LOWER_LIMIT'
                    else:
                        row['status'] = 'OPENING_RULE_MATCH'
            rows.append(row)
        matched = [x for x in rows if x['status'] == 'OPENING_RULE_MATCH']
        matched.sort(key=lambda x: (COMPONENTS[x['component']]['pri'], x['gap_pct'], x['code']))
        diagnostics = {'mode': 'RETROSPECTIVE_OPEN_REVIEW_NOT_LIVE_SIGNAL',
                       'date': now.date().isoformat(), 'checked_at': now.isoformat(),
                       'previous_signal_date': previous['date'], 'candidates': len(candidates),
                       'fresh_quote_count': len(quotes), 'opening_rule_matches': len(matched),
                       'account_A_retrospective_top2': matched[:2],
                       'account_B_prior_opening_candidates': previous.get('opening_candidates', [])[:2],
                       'stocks': rows,
                       'warning': 'Uses current quote reported open, not archived 09:25 snapshot; no alerts, orders or fills'}
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
