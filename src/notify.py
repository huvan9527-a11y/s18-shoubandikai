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
    import datetime as dt
    from zoneinfo import ZoneInfo
    delivery_day = dt.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    if sent.get('_quota_blocked_day') == delivery_day:
        print('ServerChan deferred: daily quota already exhausted; reports saved', flush=True)
        return False
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
        session = report.get("next_session") or {}
        upcoming = session.get("candidates") or []
        title = (f"S18 {day} 扫描失败" if failed else
                 f"S18 明日预选 {len(upcoming)}只｜{day}盘后" if session else
                 f"S18 {day} 历史扫描：{count}只开盘候选")
        body = (Path(root) / f"outputs/signals-{day}.md").read_text()
        body += "\n\n这是收盘后历史信号扫描，不是实时买入通知；候选不代表成交，也没有执行卖出。\n"
        run = os.getenv("GITHUB_RUN_ID")
        repo = os.getenv("GITHUB_REPOSITORY")
        if run and repo:
            body += f"\n[查看本次运行](https://github.com/{repo}/actions/runs/{run})\n"
        response = None
        try:
            response = requests.post(url, data={"title": title, "desp": body}, timeout=15)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                raise ValueError("Service rejected notification")
        except (requests.RequestException, ValueError):
            # Do not log exception strings: requests errors can contain the SendKey URL.
            status = getattr(response, 'status_code', None)
            detail = ''
            try:
                error_payload = response.json() if response is not None else {}
                if isinstance(error_payload,dict):
                    message = str(error_payload.get('message') or error_payload.get('info') or error_payload.get('msg') or '')[:180]
                    message = re.sub(r'https?://\\S+', '[redacted_url]', message.replace(key,'[redacted]'))
                    detail = f"; service_code={error_payload.get('code')}; message={message}"
                    if error_payload.get('code') == 40001 and ('发送次数' in message or 'quota' in message.lower()):
                        sent['_quota_blocked_day'] = delivery_day
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(sent, ensure_ascii=False, indent=2), encoding='utf-8')
                        temporary.replace(path)
            except Exception:
                pass
            print(f"ServerChan failed: {day}; HTTP={status}{detail}; credential redacted", flush=True)
            ok = False
            if sent.get('_quota_blocked_day') == delivery_day:
                break
            continue
        sent[day] = digest
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sent, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        print(f"ServerChan accepted: {day}", flush=True)
    return ok


def _opening_alert_log(root, date):
    path = Path(root) / "outputs" / f"opening-alert-{date}.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("date") != date or not isinstance(payload.get("attempts"), list):
            raise ValueError(f"Invalid opening alert audit log: {path.name}")
    else:
        payload = {"date": date, "attempts": []}
    return path, payload


def _save_opening_alert_log(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def push_opening_candidates(root, date, candidates, *, data_verified=False):
    """Send a timely candidate alert and persist its exact payload and outcome."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
    now = dt.datetime.now(tz)
    if now.date().isoformat() != date or not (now.hour == 9 and 25 <= now.minute <= 35):
        raise ValueError("Opening alerts require the current session at 09:25-09:35 CST")
    if not data_verified:
        raise ValueError("Live auction data has not been verified; refuse actionable alert")
    if not isinstance(candidates, list):
        raise ValueError("Candidates must be a validated list")

    title = f"S18 {date} 09:26竞价候选 {len(candidates)}只"
    lines = [f"S18 {date} 09:26竞价候选（非成交）", "",
             "A仓：09:30按已核验开盘价模拟买入；B仓：按昨日已确认信号开盘模拟买入。成交记账后另行推送。"]
    for candidate in candidates:
        lines.append(f"- {candidate['code']} {candidate.get('name', '')} {candidate['component']}")
    if not candidates:
        lines.append("竞价确认候选为零。")
    body = "\n".join(lines)
    report = dict(date=date, status="opening_verified", opening_candidates=candidates)
    digest = hashlib.sha256(json.dumps(report, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path, audit = _opening_alert_log(root, date)
    attempt = {
        "attempt": len(audit["attempts"]) + 1,
        "attempted_at": now.isoformat(),
        "data_verified": True,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "message": {"title": title, "desp": body},
        "payload_sha256": digest,
        "delivery_status": "pending",
    }
    audit["attempts"].append(attempt)
    _save_opening_alert_log(path, audit)

    def finish(status, **details):
        attempt["delivery_status"] = status
        attempt.update(details)
        _save_opening_alert_log(path, audit)

    key = (os.getenv("SERVERCHAN_SENDKEY") or os.getenv("SERVERCHAN_KEY")
           or os.getenv("SERVER_CHAN_SENDKEY") or os.getenv("SCKEY") or "").strip()
    if not key:
        finish("not_configured")
        print("Opening alert skipped: SendKey not configured", flush=True)
        return False
    try:
        url = endpoint(key)
    except ValueError:
        finish("failed", failure_reason="invalid_send_key")
        print("Opening alert failed: invalid SendKey format; credential redacted", flush=True)
        return False

    state_path = Path(root) / "state/opening_notifications.json"
    sent = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if sent.get(date) == digest:
        finish("duplicate_skipped", previously_accepted=True)
        return True

    try:
        response = requests.post(url, data={"title": title, "desp": body}, timeout=15)
        response.raise_for_status()
        response_payload = response.json()
        if not isinstance(response_payload, dict) or response_payload.get("code") != 0:
            finish("failed", failure_reason="service_rejected")
            print("Opening alert failed; credential redacted", flush=True)
            return False
    except requests.RequestException:
        finish("failed", failure_reason="request_error")
        print("Opening alert failed; credential redacted", flush=True)
        return False
    except ValueError:
        finish("failed", failure_reason="invalid_service_response")
        print("Opening alert failed; credential redacted", flush=True)
        return False

    accepted_at = dt.datetime.now(tz).isoformat()
    finish("accepted", accepted_at=accepted_at)
    sent[date] = digest
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(sent, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)
    print(f"Opening alert accepted: {date}; candidates={len(candidates)}", flush=True)
    return True


