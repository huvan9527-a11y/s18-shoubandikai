"""S18 OPP3 signals; every input ends at the previous trading day's close."""
import pandas as pd
from .paper import COMPONENTS, band

EPS = 1e-6


def checked_st(frame, pool):
    if frame is None or frame.empty:
        raise ValueError('ST data unavailable')
    row = frame.iloc[-1].reindex(pool)
    if row.isna().any():
        raise ValueError('ST data incomplete')
    return row.astype(bool)


def ice_day(first, second):
    a, b = first.set_index('code'), second.set_index('code')
    idx = b.index.intersection(a.index)
    idx = [s for s in idx if a.loc[s, 'paused'] == 0 and b.loc[s, 'paused'] == 0
           and pd.notna(a.loc[s, 'close']) and pd.notna(b.loc[s, 'close'])]
    if len(idx) < 100:
        raise ValueError('Temperature valid stock count below 100')
    ret = (b.loc[idx, 'close'] / a.loc[idx, 'close'] - 1).clip(-.2, .2)
    previous_up = a.loc[idx, 'close'] >= a.loc[idx, 'high_limit'] - EPS
    dn = float((b.loc[idx, 'close'] <= b.loc[idx, 'low_limit'] + EPS).mean())
    premium = float(ret[previous_up].mean()) if previous_up.any() else 0.
    return bool(premium <= -.01 or dn >= .01), dn


def stock_flags(frame, raw_close, date):
    df = frame.dropna(subset=['close']).copy()
    df['d'] = pd.to_datetime(df.time).dt.date
    fields = ['close', 'high_limit', 'low_limit', 'open', 'money', 'paused']
    piv = {f: df.pivot(index='code', columns='d', values=f) for f in fields}
    C = piv['close']
    cols = sorted(C.columns)
    if len(cols) < 61 or cols[-1] != date:
        raise ValueError('61-day history incomplete or stale')
    cols = cols[-61:]
    C = C.reindex(columns=cols)
    H, L, O = [piv[f].reindex(index=C.index, columns=cols) for f in ('high_limit', 'low_limit', 'open')]
    complete = C.notna().all(axis=1) & H.notna().all(axis=1) & L.notna().all(axis=1) & O.notna().all(axis=1)
    up, dn = C >= H - EPS, C <= L + EPS
    streak, active = pd.Series(0, index=C.index), pd.Series(True, index=C.index)
    for d in reversed(cols):
        m = up[d].fillna(False)
        streak += (m & active).astype(int)
        active &= m
    pos_prev = C[cols[-2]] / C[cols[:-1]].median(axis=1)
    lim = pd.Series([band(s) for s in C.index], index=C.index)
    fb = up[cols[-1]] & ~up[cols[-2]] & (pos_prev < 1) & ~(O[cols[-1]] >= H[cols[-1]] - EPS) & (lim < .15)
    out = pd.DataFrame(dict(fb=fb, lb=up[cols[-1]] & (streak >= 2),
                            gd=~up[cols[-1]] & ~dn[cols[-1]],
                            close_t=pd.Series(raw_close).reindex(C.index),
                            money=piv['money'].reindex(index=C.index, columns=cols)[cols[-1]]))
    return out[complete & (piv['paused'].reindex(index=C.index, columns=cols)[cols[-1]] == 0)].dropna()


def candidates(flags, temperature, names, min_money):
    out = []
    for code, row in flags.iterrows():
        if row.money < min_money:
            continue
        for component, rule in COMPONENTS.items():
            if temperature[rule['day']] and bool(row[rule['stock']]):
                out.append(dict(code=code, name=names[code], component=component,
                                pri=rule['pri'], prev_close=float(row.close_t), money=float(row.money)))
                break
    return out
