"""Independent deadline bulletin. Reads persisted results, never scans or trades."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def snapshot(root, kind, day):
    if kind == 's18':
        p = read(root/f'outputs/signals-{day}.json', {})
        session = p.get('next_session') or {}
        ready = p.get('date') == day and p.get('status') == 'scanned' and session.get('signal_date') == day
        return dict(date=day, kind=kind, status='ready' if ready else 'waiting',
                    count=len(session.get('candidates', [])) if ready else None,
                    candidates=session.get('candidates', []) if ready else [],
                    note='收盘候选，仍需次日竞价/开盘条件确认；不是成交。' if ready else
                    p.get('error', '当天扫描尚未完成或尚未启动，不能解释为无信号。'))
    p = read(root/'reports/screening_status.json', {})
    ready = p.get('date') == day and p.get('status') == 'provisional'
    # Preserve identifiers as strings, including Shenzhen leading zeroes.
    import csv
    path = root/'reports/current_signals.csv'
    rows = []
    if ready and path.exists():
        with path.open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
    if ready and len(rows) != p.get('signals'):
        ready = False
    return dict(date=day, kind=kind, status='provisional' if ready else 'waiting',
                count=p.get('signals') if ready else None, candidates=rows if ready else [],
                note=(p.get('note', '')+'；当前披露内初筛，不代表全市场披露完成。') if ready else
                p.get('error', '当天披露尚未取得或初筛未完成，不能解释为无信号。'))


def render(data, now):
    name = 'S18' if data['kind'] == 's18' else '龙虎榜'
    count = '待完成' if data['count'] is None else f"{data['count']}只"
    title = f"{name} {data['date']} 盘后预选：{count}"
    lines = [title, '', f"实际检查时间：{now.isoformat()}（北京时间）；目标首报17:00。",
             f"状态：{data['status']}", '', data['note'], '']
    if data['count'] == 0:
        lines.append('当前已验证范围内无候选。' if data['status'] == 'provisional' else '当天已完成扫描，候选为零。')
    for row in data['candidates'][:30]:
        lines.append(f"- {row.get('code','')} {row.get('name','')} {row.get('component','')}")
    repo = 'huvan9527-a11y/' + ('s18-shoubandikai' if data['kind']=='s18' else 'longhu')
    file = f"outputs/signals-{data['date']}.md" if data['kind']=='s18' else 'reports/latest_signals.md'
    lines += ['', f'[完整结果](https://github.com/{repo}/blob/main/{file})',
              '扫描、状态通知和晚间模拟记账独立运行。']
    return title, '\n'.join(lines)+'\n'


def endpoint(key):
    if re.fullmatch(r'SCT[A-Za-z0-9]+', key):
        return f'https://sctapi.ftqq.com/{key}.send'
    match = re.fullmatch(r'sctp(\d+)t[A-Za-z0-9]+', key)
    if match:
        return f'https://{match[1]}.push.ft07.com/send/{key}.send'
    raise ValueError('invalid_key_format')


def deliver(key, title, body):
    request = urllib.request.Request(endpoint(key), data=urllib.parse.urlencode(dict(title=title, desp=body)).encode())
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def run(kind, phase='initial', root=ROOT, now=None):
    now = now or dt.datetime.now(ZoneInfo('Asia/Shanghai'))
    if now.weekday() >= 5 or (now.hour, now.minute) < (17, 0):
        print('POSTCLOSE_SKIP: 尚未到首报时间或周末')
        return
    day = now.date().isoformat()
    data = snapshot(root, kind, day)
    title, body = render(data, now)
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = root/'state/postclose_notifications.json'
    state = read(path, {})
    current = state.setdefault(day, {'accepted': [], 'attempts': []})
    legacy = read(root/'state/notifications.json', {})
    if legacy.get('_quota_blocked_day') == day:
        current['quota_blocked'] = True
    # Only acknowledged payloads deduplicate; an undelivered initial remains retryable.
    if current['accepted'] and current['accepted'][-1]['digest'] == digest:
        print('POSTCLOSE_UNCHANGED: 已确认的相同结果不重复推送')
        return
    attempt = dict(at=now.isoformat(), phase=phase, digest=digest, delivery='pending')
    current['attempts'].append(attempt)
    data.update(checked_at=now.isoformat(), target_first_report='17:00', phase=phase)
    save(root/f'outputs/postclose-{day}.json', data)
    (root/f'outputs/postclose-{day}.md').write_text(body)
    summary = os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write(body)
    key = next((os.getenv(k, '').strip() for k in ('SERVERCHAN_SENDKEY','SERVERCHAN_KEY','SERVER_CHAN_SENDKEY','SCKEY','SENDKEY') if os.getenv(k, '').strip()), '')
    if not key:
        attempt['delivery'] = 'not_configured'
    elif current.get('quota_blocked'):
        attempt['delivery'] = 'quota_blocked'
    else:
        try:
            reply = deliver(key, title, body)
            if not isinstance(reply, dict) or reply.get('code') != 0:
                attempt['delivery'] = 'rejected'
                attempt['service_code'] = reply.get('code') if isinstance(reply,dict) else None
                if isinstance(reply,dict) and reply.get('code') == 40001 and any(x in str(reply.get('message') or reply.get('info') or reply.get('msg') or '').lower() for x in ('次数','额度','quota')):
                    current['quota_blocked'] = True
            else:
                attempt['delivery'] = 'accepted'
                current['accepted'].append(dict(digest=digest, at=now.isoformat(), status=data['status']))
        except Exception as exc:
            attempt.update(delivery='failed', error_type=type(exc).__name__)
    save(path, state)
    print('POSTCLOSE_DELIVERY', attempt['delivery'], flush=True)
    if attempt['delivery'] != 'accepted':
        print('::warning::盘后状态已保存，推送未确认；查看state/postclose_notifications.json。不能当成已送达。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--kind', choices=['s18','longhu'], required=True)
    parser.add_argument('--phase', choices=['initial','update'], default='initial')
    args = parser.parse_args()
    run(args.kind, args.phase)
