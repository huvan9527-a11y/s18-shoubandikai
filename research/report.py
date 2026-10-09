"""Independent result audit and Chinese comparison report."""
import json
from pathlib import Path
from collections import Counter
import numpy as np

LABELS={'open_D1_open':'开盘买→次日开盘卖','open_D1_close':'开盘买→次日收盘卖',
        'open_D2_close':'开盘买→第三交易日收盘卖','close_D1_open':'收盘买→次日开盘卖',
        'close_D1_close':'收盘买→次日收盘卖','close_D2_close':'收盘买→第三交易日收盘卖'}
def pct(x):return '—' if x is None else f'{x*100:.2f}%'

def generate(path):
    x=json.loads(Path(path).read_text()); events=x['signals']; trades=x['trades']; portfolios=x['portfolios']
    assert len({(e['date'],e['code']) for e in events})==len(events)
    assert len(trades)==len(events)*6
    daily=x['daily_scans']; assert sum(d['opening_signals'] for d in daily)==len(events)
    assert all(x['period'][0]<=d['date']<=x['period'][1] for d in daily)
    assert len({d['date'] for d in daily})==len(daily)
    assert all(t['exit_date']>t['date'] for t in trades if t['status']=='closed')
    schemes=list(LABELS)
    closed={(t['date'],t['code'],t['scheme']):t for t in trades if t['status']=='closed'}
    common=[e for e in events if all((e['date'],e['code'],s) in closed for s in schemes)]
    for p in portfolios:
        values=np.array([200000]+[d['equity'] for d in p['equity']])
        assert abs(values[-1]-p['final_equity'])<.001
        assert abs((values/np.maximum.accumulate(values)-1).min()-p['max_drawdown'])<1e-10
        assert all(0<=d['positions']<=2 and d['cash']>=-.001 for d in p['equity'])
        assert all(t['entry_date']<t['exit_date'] for t in p['fills'])
    parts=Counter(e['component'] for e in events)
    lines=['# S18 六种买卖方案历史比较',
           f"\n期间：{x['period'][0]}—{x['period'][1]}。已扫描{len(daily)}个交易日，首次实际扫描日{daily[0]['date']}，共{len(events)}个开盘低开信号。",
           '\n**数据性质：历史月度股票池近似回测，不能称为聚宽原口径的精确历史复现。**',
           '\n## 口径',
           '\n- 信号日记为D，次日为D+1；第三交易日指D+2，包含买入日计数。',
           '- 开盘与收盘买入使用同一批开盘低开合格信号，完整包括FB_dip、LB、DEEP三个组件。',
           '- 尾盘买卖统一用日线收盘价代理，不需要分钟K，也无法验证某一分钟的实际成交。',
           '- 月度成分快照来自yfiua/index-constituents的中证指数历史存档。仅从下一月起使用，避免误用月内后来更新内容，但会延迟反映成分调整；历史月内ST变化仍缺失。',
           '- 全部历史池并集1209只。新浪日线及复权因子、中证1000腾讯指数日线，历史数据从2025年初补足。',
           '- 买入信号日除权排除。持仓内除权使用复权因子收益代理，未精确模拟分红现金到账、税及送转股份。',
           '- 每边滑点0.1%，每边佣金0.025%，卖出印花税0.05%。逐信号收益为价格比例研究口径，不计5元最低佣金和整手约束；组合另计。',
           '- 次日不能卖出的跌停/停牌，按原定开盘或收盘时点延后至可卖日。收盘涨停不假设买到，期末未到卖出日或仍不可卖为未完成。',
           '- 20万元组合最多两只持仓，每日最多新买两只，按组件优先级、低开幅度、代码排序；每笔最多半仓，留1%缓冲，且不超过前日成交额0.1%，整手成交，最低佣金5元。',
           '- 组合回撤按每日收盘净值计算，包含期末未卖持仓市值，不代表盘中最大回撤。原20万元模拟仓账本未修改。',
           '\n组件信号数：'+ '，'.join(f'{k} {v}个' for k,v in sorted(parts.items()))+'。',
           '\n## 全量逐信号表现',
           '\n独立事件样本允许重叠，平均收益不等于账户总收益。',
           '\n|方案|信号总数|已完成|未完成|买不到|平均净收益|中位数|净盈利胜率|延后卖出|',
           '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    byscheme={s['scheme']:s for s in x['summary']}
    for scheme in schemes:
        s=byscheme[scheme]
        lines.append(f"|{LABELS[scheme]}|{s['all_signals']}|{s['closed']}|{s['pending']}|{s['unfilled']}|{pct(s['avg_return'])}|{pct(s['median_return'])}|{pct(s['win_rate'])}|{s['delayed']}|")
    lines+=['\n## 相同可完成样本对照',f'\n以下仅保留六组均可买入且卖出完成的同一批{len(common)}个信号，用于公平比较时点差异。',
            '\n|方案|平均净收益|中位数|净盈利胜率|首板样本数|首板平均净收益|',
            '|---|---:|---:|---:|---:|---:|']
    paired=[]
    for scheme in schemes:
        ts=[closed[(e['date'],e['code'],scheme)] for e in common]
        rs=np.array([t['net_return'] for t in ts]); fbs=np.array([t['net_return'] for t in ts if t['component']=='FB_dip'])
        metrics=dict(scheme=scheme,count=len(rs),avg_return=float(rs.mean()) if len(rs) else None,
                     fb_count=len(fbs),fb_avg=float(fbs.mean()) if len(fbs) else None)
        paired.append(metrics)
        lines.append(f"|{LABELS[scheme]}|{pct(metrics['avg_return'])}|{pct(float(np.median(rs)) if len(rs) else None)}|{pct(float((rs>0).mean()) if len(rs) else None)}|{len(fbs)}|{pct(metrics['fb_avg'])}|")
    lines+=['\n## 20万元两持仓组合',
            '\n|方案|期末净值（元）|期间收益|最大收盘回撤|已平仓笔数|平仓胜率|期末持仓|',
            '|---|---:|---:|---:|---:|---:|---:|']
    byportfolio={p['scheme']:p for p in portfolios}
    for scheme in schemes:
        p=byportfolio[scheme]
        lines.append(f"|{LABELS[scheme]}|{p['final_equity']:,.2f}|{pct(p['total_return'])}|{pct(p['max_drawdown'])}|{p['closed_trades']}|{pct(p['win_rate'])}|{p['open_positions']}|")
    fb_path=Path(path).parent/'first-board-portfolios.json'
    if fb_path.exists():
        fbps=json.loads(fb_path.read_text())
        lines+=['\n## 只做首板低开（43个信号）的20万元组合',
                '\n与完整S18相同资金、仓位和成本约束，单独排除LB和DEEP组件。',
                '\n|方案|期末净值（元）|期间收益|最大收盘回撤|已平仓笔数|期末持仓|',
                '|---|---:|---:|---:|---:|---:|']
        for p in fbps:
            lines.append(f"|{LABELS[p['scheme']]}|{p['final_equity']:,.2f}|{pct(p['total_return'])}|{pct(p['max_drawdown'])}|{p['closed_trades']}|{p['open_positions']}|")
    best=max(portfolios,key=lambda p:p['total_return'])
    top=sorted(best['fills'],key=lambda t:t['profit'],reverse=True)[:5]
    contribution=sum(t['profit'] for t in top)/(best['final_equity']-200000)
    lines+=['\n## 收益集中度',
            f"\n完整S18最好方案为{LABELS[best['scheme']]}，实际选择{best['closed_trades']}笔已平仓交易。盈利最多的五笔贡献净利润的{pct(contribution)}；该比例以含亏损抵扣后的净利润为分母。这些结果受短样本、选股排序和月度股票池近似影响。",
            '\n|代码|买入日期|卖出日期|组件|净盈利（元）|', '|---|---|---|---|---:|']
    for t in top:lines.append(f"|{t['code']}|{t['entry_date']}|{t['exit_date']}|{t['component']}|{t['profit']:,.2f}|")
    lines+=['\n## 各组件逐信号表现',
            '\n|组件|方案|已完成数|平均净收益|净盈利胜率|', '|---|---|---:|---:|---:|']
    component_summary=[]
    for comp in sorted(parts):
        for scheme in schemes:
            rs=np.array([t['net_return'] for t in trades if t['component']==comp and t['scheme']==scheme and t['status']=='closed'])
            mean=float(rs.mean()) if len(rs) else None;win=float((rs>0).mean()) if len(rs) else None
            component_summary.append(dict(component=comp,scheme=scheme,closed=len(rs),avg_return=mean,win_rate=win))
            lines.append(f'|{comp}|{LABELS[scheme]}|{len(rs)}|{pct(mean)}|{pct(win)}|')
    lines+=['\n## 最近信号明细','\n|信号日|代码|名称|组件|开盘价|低开幅度|', '|---|---|---|---|---:|---:|']
    for e in events[-30:]: lines.append(f"|{e['date']}|{e['code']}|{e['name']}|{e['component']}|{e['open']:.2f}|{pct(e['gap'])}|")
    lines+=['\n全量逐信号、六组逐笔、每日扫描温度、每日组合净值、组合成交及未买原因见配套JSON明细。',
            '\n## 数据来源',
            '\n- https://github.com/yfiua/index-constituents',
            '- https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000852cons.xls',
            '- 新浪财经历史日线及复权因子（AKShare公开接口地址）',
            '- https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
            '\n## 核验',
            '\n逐日信号数与总明细相符；每个信号恰有六组结果；成交卖出日期均晚于买入日期；组合现金非负、持仓不超过两只；最大回撤和期末净值独立重算一致。']
    out=Path(path).parent
    (out/'S18-comparison-report.md').write_text('\n'.join(lines),encoding='utf-8')
    (out/'additional-analysis.json').write_text(json.dumps(dict(paired=paired,component_summary=component_summary),ensure_ascii=False,indent=2))
    print(json.dumps(dict(signals=len(events),trading_days=len(daily),components=parts,paired=paired,
                         portfolios=[{k:v for k,v in p.items() if k not in ('equity','fills','skips')} for p in portfolios]),ensure_ascii=False,indent=2))

if __name__=='__main__':
    import sys
    generate(sys.argv[1])
