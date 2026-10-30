"""Public-data reference scan. Does not mutate the authoritative account."""
import argparse
import datetime as dt
import json
import time
from zoneinfo import ZoneInfo
from .data import PublicData, ROOT
from .paper import DEFAULTS, new_account
from .run import write_json
from .notify import push_reports

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--dates', nargs='+')
    parser.add_argument('--end-date')
    parser.add_argument('--no-notify', action='store_true', help='Persist reports only; independent deadline task handles notifications')
    parser.add_argument('--latest-notification', action='store_true', help='Only notify the newest report; keep both reports on disk')
    parser.add_argument('--notify-at', help='Beijing HH:MM; scheduled scan prepares first, then sends at this time')
    args = parser.parse_args(argv)
    provider = PublicData()
    cfg = DEFAULTS | json.loads((ROOT / 'config.json').read_text())
    account = new_account(cfg['start_date'])
    now = dt.datetime.now(ZoneInfo('Asia/Shanghai'))
    end = now.date() if now.hour >= 16 else now.date()-dt.timedelta(days=1)
    through = dt.date.fromisoformat(args.end_date) if args.end_date else end
    if through > end:
        raise ValueError('Future/incomplete day prohibited')
    dates = [dt.date.fromisoformat(x) for x in args.dates] if args.dates else provider.days(end_date=through, count=1)
    failed = False
    reports = []
    for day in dates:
        if day > end:
            raise ValueError('Future/incomplete day prohibited')
        try:
            report = provider.scan(day, account, cfg)
        except Exception as e:
            failed = True
            report = dict(date=str(day), status='failed_data_incomplete', error=f'{type(e).__name__}: {e}', stock_errors=provider.errors,
                          candidates=None, opening_candidates=None)
        write_json(ROOT / f'outputs/signals-{day}.json', report)
        lines = [f'# S18 {day} 公共行情扫描', '', f'- 状态：{report["status"]}', '- 本扫描不修改20万元模拟仓账本。']
        if 'error' in report:
            lines += ['- 错误：'+report['error'], '- 未完成扫描，不能解释为零信号。']
        else:
            lines += ['- 股票池：'+str(report['pool_size']), '- 温度：`'+json.dumps(report['temperature'])+'`']
            if cfg.get('fb_expand_mainboard'):
                lines += ['- FB_dip候选池：原池＋沪深全主板非ST；LB、DEEP与市场温度维持原池。']
            lines += ['- '+x for x in report['limitations']]
            lines += ['', '|股票|名称|组件|开盘低开幅度|', '|---|---|---|---:|']
            for c in report['opening_candidates']:
                lines.append(f'|{c["code"]}|{c["name"]}|{c["component"]}|{(c["open"]/c["prev_close"]-1)*100:.2f}%|')
            if not report['opening_candidates']:
                lines.append('|—|无开盘候选|—|—|')
            lines += ['', f'前一日候选 {len(report["candidates"])} 只；开盘候选 {len(report["opening_candidates"])} 只。']
            session = report.get('next_session')
            if session:
                lines += ['', f"下一交易日候选依据 {session['signal_date']} 收盘：{len(session['candidates'])} 只。",
                          '|股票|名称|组件|信号日原始收盘价|', '|---|---|---|---:|']
                lines += [f"|{c['code']}|{c['name']}|{c['component']}|{c['signal_close']:.2f}|" for c in session['candidates']]
        (ROOT / f'outputs/signals-{day}.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
        print('\n'.join(lines), flush=True)
        reports.append(report)
    if args.no_notify:
        write_json(ROOT / 'outputs/scan-delivery.json', dict(scan_failed=failed, notification_delegated=True))
        return int(failed)
    if args.notify_at:
        hh, mm = map(int,args.notify_at.split(':'))
        target = dt.datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=hh,minute=mm,second=0,microsecond=0)
        while dt.datetime.now(target.tzinfo) < target:
            time.sleep(min(10,(target-dt.datetime.now(target.tzinfo)).total_seconds()))
    notified = push_reports(ROOT, reports[-1:] if args.latest_notification else reports)
    write_json(ROOT / 'outputs/scan-delivery.json', dict(scan_failed=failed, notification_delivered=bool(notified)))
    if not notified:
        print('::warning::扫描结果已保存；通知未送达，详见outputs/scan-delivery.json。不会因此重做扫描。', flush=True)
    return int(failed)

if __name__ == '__main__':
    raise SystemExit(main())



