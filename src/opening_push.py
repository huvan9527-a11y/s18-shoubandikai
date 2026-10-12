"""09:26 CST verified Tencent auction snapshots -> ServerChan candidates, never fills."""
import datetime as dt
import json
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
    for c in candidates:
        q = quote.get(c["code"])
        if not q:
            raise ValueError("Missing fresh 09:25 auction quote: " + c["code"])
        if abs(q["prev_close"] / c["prev_close"] - 1) > .003:
            raise ValueError("Auction previous close mismatch: " + c["code"])
        from .paper import band
        rate = band(c["code"])
        marks[c["code"]] = dict(open=q["open"], high_limit=price_limit(q["prev_close"], rate),
                                low_limit=price_limit(q["prev_close"], -rate),
                                paused=False, is_st=bool(re.search("ST|退", q["name"], re.I)),
                                name=q["name"])
    return opening_queue(candidates, marks, cfg)


def main():
    now = dt.datetime.now(TZ)
    if now.weekday() >= 5 or now.hour != 9 or not 25 <= now.minute <= 35:
        raise ValueError("09:26 alert must run in live morning window")
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
