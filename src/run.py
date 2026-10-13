"""S18 GitHub Actions entrypoint and atomic, restartable paper ledger."""
import argparse
import copy
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from .paper import DEFAULTS, new_account, replay_day, total_value
from .data import PublicData

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def export(account, root, status, detail=''):
    write_json(root / 'state/account.json', account)
    write_csv(root / 'logs/trades.csv', account['trades'],
              ['date', 'time', 'code', 'side', 'amount', 'price', 'gross', 'commission', 'tax', 'realised', 'component', 'model'])
    write_csv(root / 'logs/equity.csv', account['equity'], ['date', 'cash', 'market_value', 'equity', 'return_pct'])
    held = [dict(code=code, **p) for code, p in account['positions'].items()]
    write_csv(root / 'outputs/holdings.csv', held, ['code', 'name', 'component', 'entry_date', 'amount', 'cost', 'mark', 'factor'])
    last = account['days'][-1] if account['days'] else {}
    write_json(root / 'outputs/latest.json', dict(status=status, detail=detail, last_day=last,
                                                  cash=account['cash'], positions=account['positions'],
                                                  equity=round(total_value(account), 2)))
    report = [
        '# S18 OPP3 · 20万元模拟仓', '',
        f'- 状态：`{status}`' + (f'；{detail}' if detail else ''),
        f'- 启动日期：{account["start_date"]}；已处理至：{account["last_processed"] or "尚未开始"}',
        f'- 初始资金：{account["initial_cash"]:,.2f} 元',
        f'- 可用现金：{account["cash"]:,.2f} 元',
        f'- 账户权益：{total_value(account):,.2f} 元',
        f'- 累计收益：{(total_value(account) / account["initial_cash"] - 1) * 100:.2f}%',
        f'- 累计模拟成交：{len(account["trades"])} 笔', '',
        '公共行情参考口径：历史成分及ST状态未完整还原。免费分钟源仅保留近期交易日；无完整分钟数据时不计成交，也不补造历史。',
        '所有成交均为撮合模型假设，不是券商成交回报。',
        '查看明细：`outputs/latest.json`、`outputs/holdings.csv`、`logs/trades.csv`、`logs/equity.csv`。', '',
        '| 股票 | 名称 | 组件 | 股数 | 买入日 | 含费成本 | 最新价 |',
        '|---|---|---|---:|---|---:|---:|',
    ]
    for code, p in account['positions'].items():
        report.append(f'| {code} | {p["name"]} | {p["component"]} | {p["amount"]} | {p["entry_date"]} | {p["cost"]:.2f} | {p["mark"]:.2f} |')
    if not account['positions']:
        report.append('| — | 空仓 | — | 0 | — | — | — |')
    if last.get('preparation_error'):
        report += ['', '当日新增买入已暂停：' + last['preparation_error']]
    (root / 'outputs/latest.md').write_text('\n'.join(report) + '\n', encoding='utf-8')


def process(provider, account, cfg, end_date, root):
    start = dt.date.fromisoformat(account['start_date'])
    if account['last_processed']:
        start = dt.date.fromisoformat(account['last_processed']) + dt.timedelta(days=1)
    if start > end_date:
        export(account, root, 'up_to_date')
        return
    days = provider.days(start_date=start, end_date=end_date)
    for day in days:
        if day < start or day > end_date:
            continue
        preparation_error = None
        temperature = None
        try:
            candidates, temperature = provider.prepare(day, account, cfg)
        except Exception as e:
            # Failed preparation cannot stop liquidation of existing positions.
            if not account['positions']:
                raise
            candidates = []
            preparation_error = f'{type(e).__name__}: 准备行情失败，禁止新增买入；请查看公开行情接口、网络和完整性。'
        try:
            daily, minute = provider.session(day, candidates, account['positions'], cfg)
        except Exception:
            if not candidates or not account['positions']:
                raise
            # If the buy-data branch fails, request held-stock data independently.
            candidates = []
            preparation_error = '买入行情不完整，禁止新增；旧持仓独立处理。'
            daily, minute = provider.session(day, [], account['positions'], cfg)
        replay_day(account, day.isoformat(), candidates, daily, minute, cfg, preparation_error)
        for code, p in account['positions'].items():
            if 'factor' in daily[code]:
                p['factor'] = daily[code]['factor']
        account['days'][-1]['temperature'] = temperature
        export(account, root, 'degraded_no_new_buys' if preparation_error else 'processed')
        print(f'{day}: positions={len(account["positions"])} equity={total_value(account):.2f}'
              + (' WARNING: new buys disabled' if preparation_error else ''))
    if not days:
        export(account, root, 'non_trading_day')


def process_report(provider, account, cfg, report, root):
    """Replay exactly one successful scan date; failed preflight never touches ledger exports."""
    root = Path(root)
    if report.get('status') != 'scanned' or not report.get('date'):
        write_json(root / 'outputs/paper-status.json', dict(status='scan_not_successful',
                   detail='扫描未成功；模拟账本未更新。', report_date=report.get('date')))
        return 'scan_not_successful'
    day = dt.date.fromisoformat(report['date'])
    if account['last_processed']:
        last = dt.date.fromisoformat(account['last_processed'])
        if day <= last:
            return 'up_to_date'
        try:
            intervening = provider.days(start_date=last + dt.timedelta(days=1), end_date=day)
        except Exception as exc:
            write_json(root / 'outputs/paper-status.json', dict(status='calendar_unavailable',
                       detail=f'{type(exc).__name__}: {str(exc)[:250]}；账本保持不变。',
                       last_processed=account['last_processed'], requested_date=str(day)))
            return 'calendar_unavailable'
        if intervening != [day]:
            write_json(root / 'outputs/paper-status.json', dict(status='missed_trading_day',
                       detail='检测到未处理交易日；为避免伪造分钟成交，账本保持不变。',
                       last_processed=account['last_processed'], requested_date=str(day)))
            return 'missed_trading_day'
    candidates = report.get('opening_candidates')
    if not isinstance(candidates, list):
        write_json(root / 'outputs/paper-status.json', dict(status='invalid_scan_report',
                   detail='扫描报告缺少有效开盘候选列表；账本保持不变。', report_date=str(day)))
        return 'invalid_scan_report'
    working = copy.deepcopy(account)
    if not working['last_processed'] and not working['days'] and not working['trades'] and not working['positions']:
        working['start_date'] = str(day)
    try:
        daily, minutes = provider.session(day, candidates, working['positions'], cfg)
        if not replay_day(working, str(day), candidates, daily, minutes, cfg):
            return 'up_to_date'
    except Exception as exc:
        write_json(root / 'outputs/paper-status.json', dict(status='invalid_session',
                   detail=f'{type(exc).__name__}: {str(exc)[:300]}；账本保持不变。', report_date=str(day)))
        return 'invalid_session'
    for code, position in working['positions'].items():
        if 'factor' in daily[code]:
            position['factor'] = daily[code]['factor']
    if working['days']:
        working['days'][-1]['temperature'] = report.get('temperature')
    new_trades = working['trades'][len(account['trades']):]
    export(working, root, 'processed')
    (root / 'outputs/paper-status.json').unlink(missing_ok=True)
    account.clear()
    account.update(working)
    fills = ','.join(f"{trade['side']}:{trade['code']}:{trade['amount']}" for trade in new_trades) or 'none'
    print(f'Paper ledger processed: date={day}, new_fills={len(new_trades)}, fills={fills}, '
          f'positions={len(working["positions"])}, equity={total_value(working):.2f}', flush=True)
    return 'processed'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--end-date', help='Optional completed date; no future/incomplete day allowed')
    parser.add_argument('--initialize', action='store_true', help='Create only if no account exists; never reset')
    parser.add_argument('--paper-from-scan', action='store_true', help='Replay the newest successful daily scan')
    args = parser.parse_args()
    cfg = DEFAULTS | json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    path = ROOT / 'state/account.json'
    account = json.loads(path.read_text(encoding='utf-8')) if path.exists() else new_account(cfg['start_date'])
    if cfg['initial_cash'] != 200000 or account['initial_cash'] != 200000 or cfg['slots'] != 2 or cfg['max_new'] != 2:
        raise ValueError('本模拟仓固定20万元、2槽、每日最多2只；不能热改初始资金。')
    if args.initialize:
        export(account, ROOT, 'waiting_for_configuration')
        return 0
    if args.paper_from_scan:
        reports = sorted((ROOT / 'outputs').glob('signals-????-??-??.json'), reverse=True)
        if not reports:
            write_json(ROOT / 'outputs/paper-status.json', dict(status='scan_missing',
                       detail='未找到扫描报告；模拟账本未更新。'))
            return 1
        report = json.loads(reports[0].read_text(encoding='utf-8'))
        status = process_report(PublicData(), account, cfg, report, ROOT)
        print(f'Paper ledger: {status}', flush=True)
        return 0 if status in ('processed', 'up_to_date') else 1
    # The active strategy only scans daily/opening conditions; no minute replay.
    from .scan import main as scan_main
    return scan_main(['--end-date', args.end_date] if args.end_date else [])


if __name__ == '__main__':
    raise SystemExit(main())
