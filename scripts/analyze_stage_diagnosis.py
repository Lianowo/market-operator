"""链路逐段诊断(任务#7): 识别→行为→决策→持仓, 用既有 trades CSV + 1h OHLCV 逐笔复算。
只读产物, 不产生任何新判定, 不接触 test 结论之外的衡量(属对已冻结产物的归因分析)。"""
import pandas as pd, numpy as np, sys

def load_bars(sym):
    df = pd.read_csv(f'data/raw/{sym}_1h.csv')
    return df.reset_index(drop=True)

def diag(trades_path, label):
    t = pd.read_csv(trades_path)
    bars = {s: load_bars(s) for s in t.symbol.unique()}
    idx = {s: {ts: i for i, ts in enumerate(bars[s].ts)} for s in bars}
    rows = []
    for tr in t.itertuples():
        b = bars[tr.symbol]; ix = idx[tr.symbol]
        e_i, x_i = ix[tr.open_ts], ix[tr.exit_ts]
        R = abs(tr.entry - tr.invalid)
        seg = b.iloc[e_i:x_i + 1]
        d = -1 if tr.direction == -1 else 1
        mfe = d * (seg.high.max() - tr.entry) if d == 1 else d * (tr.entry - seg.low.min())
        mfe_R = mfe / R
        # 止损出局后 24 根内是否回到入场价之上(噪声止损)
        bounces = np.nan
        if abs(tr.r_multiple) > 0 and tr.r_multiple < 0 and x_i + 1 < len(b):
            after = b.iloc[x_i + 1:x_i + 25]
            if len(after):
                rec = (after.close > tr.entry).any() if d == 1 else (after.close < tr.entry).any()
                bounces = bool(rec)
        # 退出后 48 根内是否触及原目标(过早退出懊悔)
        regret = np.nan
        if tr.target_1 == tr.target_1:
            after48 = b.iloc[x_i + 1:x_i + 49]
            if len(after48):
                hit = (after48.low <= tr.target_1).any() if d == -1 else (after48.high >= tr.target_1).any()
                regret = bool(hit)
        rows.append(dict(mfe_R=mfe_R, fin_R=tr.r_multiple, giveback_R=mfe_R - tr.r_multiple,
                         bounce=bounces, regret=regret, exit=tr.reason_exit.split('|')[0],
                         event=tr.event_id))
    q = pd.DataFrame(rows)
    print(f'==== {label} ====')
    print(f'n={len(q)}  MFE_R中位 {q.mfe_R.median():.2f}  实现R中位 {q.fin_R.median():.2f}  回吐R中位 {q.giveback_R.median():.2f}')
    print(f' MFE_R>=1 但最终<=0(到嘴又吐光): {((q.mfe_R>=1)&(q.fin_R<=0)).mean()*100:.1f}%   MFE_R>=2 但最终<1: {((q.mfe_R>=2)&(q.fin_R<1)).mean()*100:.1f}%')
    bs = q.bounce.dropna(); print(f' 亏损单 24h 内价格回到入场对侧(噪声止损): {bs.mean()*100:.1f}% (n={len(bs)})')
    rg = q.regret.dropna(); print(f' 退出后 48h 内触及原目标(过早退出): {rg.mean()*100:.1f}% (n={len(rg)})')
    print(' 按退出规则:  回吐R中位 / 实现R均值')
    print(q.groupby('exit').agg(gbR=('giveback_R','median'), finR=('fin_R','mean'), n=('fin_R','size')).round(2))

diag('reports/trades_full.csv', 'v1 全期(市价追价入场)')
diag('reports/trades_v5_full.csv', 'v5 全期(质量路由)')
