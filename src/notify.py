"""ServerChan scan summaries; never describe candidates as executed orders."""
import hashlib
import json
import os
import re
from pathlib import Path

import requests


def endpoint(key):
    if re.fullmatch(r"SCT[A-Za-z0-9]+", key):
        return f"https://sctapi.ftqq.com/{key}.send"
    match = re.fullmatch(r"sctp(\d+)t[A-Za-z0-9]+", key)
    if match:
        return f"https://{match[1]}.push.ft07.com/send/{key}.send"
    raise ValueError("Unsupported ServerChan SendKey format")


def push_reports(root, reports):
    key = (os.getenv("SERVERCHAN_SENDKEY") or os.getenv("SERVERCHAN_KEY")
           or os.getenv("SERVER_CHAN_SENDKEY") or os.getenv("SCKEY") or "").strip()
    if not key:
        print("ServerChan skipped: SendKey not configured", flush=True)
        return True
    # Validate without printing a URL containing the credential.
    try:
        url = endpoint(key)
    except ValueError:
        print("ServerChan failed: invalid SendKey format", flush=True)
        return False
    path = Path(root) / "state/notifications.json"
    sent = json.loads(path.read_text()) if path.exists() else {}
    ok = True
    for report in reports:
        day = report["date"]
        raw = json.dumps(report, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if sent.get(day) == digest:
            print(f"ServerChan duplicate skipped: {day}", flush=True)
            continue
        failed = report["status"] != "scanned"
        count = len(report.get("opening_candidates") or [])
        title = f"S18 {day} 扫描失败" if failed else f"S18 {day} 历史扫描：{count}只开盘候选"
        body = (Path(root) / f"outputs/signals-{day}.md").read_text()
        body += "\n\n这是收盘后历史信号扫描，不是实时买入通知；候选不代表成交，也没有执行卖出。\n"
        run = os.getenv("GITHUB_RUN_ID")
        repo = os.getenv("GITHUB_REPOSITORY")
        if run and repo:
            body += f"\n[查看本次运行](https://github.com/{repo}/actions/runs/{run})\n"
        try:
            response = requests.post(url, data={"title": title, "desp": body}, timeout=15)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                raise ValueError("Service rejected notification")
        except (requests.RequestException, ValueError):
            # Do not log exception strings: requests errors can contain the SendKey URL.
            print(f"ServerChan failed: {day}; credential redacted", flush=True)
            ok = False
            continue
        sent[day] = digest
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sent, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        print(f"ServerChan accepted: {day}", flush=True)
    return ok
