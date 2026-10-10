"""Public-data reference scan. Does not mutate the authoritative account."""
import argparse
import datetime as dt
import json
from zoneinfo import ZoneInfo
from .data import PublicData, ROOT
from .paper import DEFAULTS, new_account
from .run import write_json
from .notify import push_reports

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--dates', nargs='+')
    parser.add_argument('--end-date')
    args = parser.parse_args(argv)
    provider = PublicData()
    cfg = DEFAULTS | json.loads((ROOT / 'config.json').read_text())
    account = new_account(cfg['start_date'])
    now = dt.datetime.now(ZoneInfo('Asia/Shanghai'))
    end = now.date() if now.hour >= 16 else now.date()-dt.timedelta(days=1)
    through = dt.date.fromisoformat(args.end_date) if args.end_date else end
    if through > end:
        raise ValueError('Future/incomplete day prohibited')
    dates = [dt.date.fromisoformat(x) for x in args.dates] if args.dates else provider.days(end_date=through, count=2)
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
            lines += ['- '+x for x in report['limitations']]
            lines += ['', '|股票|名称|组件|开盘低开幅度|', '|---|---|---|---:|']
            for c in report['opening_candidates']:
                lines.append(f'|{c["code"]}|{c["name"]}|{c["component"]}|{(c["open"]/c["prev_close"]-1)*100:.2f}%|')
            if not report['opening_candidates']:
                lines.append('|—|无开盘候选|—|—|')
            lines += ['', f'前一日候选 {len(report["candidates"])} 只；开盘候选 {len(report["opening_candidates"])} 只。']
        (ROOT / f'outputs/signals-{day}.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
        print('\n'.join(lines), flush=True)
        reports.append(report)
    notified = push_reports(ROOT, reports)
    return int(failed or not notified)

if __name__ == '__main__':
    raise SystemExit(main())

