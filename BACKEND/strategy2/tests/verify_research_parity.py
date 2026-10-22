"""Optional local target parity check; requires trusted frozen research files.

Does not download prices, simulate accounts, start the Bot, or place orders.
"""
import argparse
import hashlib,json,pickle
from pathlib import Path
import numpy as np
import pandas as pd
from sgs import regime_v5 as rv
from sgs.live import TICKERS, VERSION, cfg, income_sleeve, risk_scale, target_weights


def verify(research, output):
    cache=research/'schd20_robust_results/cache/long.pkl'
    reference=research.parent/'reports/data_balanced15/cache/ledger_actual_base.parquet'
    provenance=research.parent/'reports/data_balanced15/PROVENANCE.json'
    assert hashlib.sha256(cache.read_bytes()).hexdigest()==json.loads(provenance.read_text())['inputs']['research/schd20_robust_results/cache/long.pkl']
    with cache.open('rb') as f:
        data=pickle.load(f)
    ledger=pd.read_parquet(reference)
    bars,signals,_,_=data['market']
    first,end=ledger.index[0],ledger.index[-1]
    close=bars.pivot(index='session',columns='symbol',values='close')[list(TICKERS)]
    split=bars.pivot(index='session',columns='symbol',values='split_ratio')[list(TICKERS)].fillna(1)
    dividend=bars.pivot(index='session',columns='symbol',values='dividend')[list(TICKERS)].fillna(0)
    gross=split*(close+dividend)/close.shift()
    for ticker in TICKERS:
        gross.loc[close[ticker].first_valid_index(),ticker]=1
    tr=gross.cumprod()
    config=cfg(); state=rv.V5StreakState(); checked=0; error=0.; regimes={}
    q=signals['QQQ']; sp=signals['SPMO']
    qvalues=q.to_numpy(float); spvalues=sp.to_numpy(float)
    for i,day in enumerate(q.index):
        if day>end:
            break
        bucket,qsig=rv.step_v5(config,state,qvalues[:i])
        if bucket is None:
            continue
        slot,ssig=rv.spmo_slot_signal(config,state,spvalues[:sp.index.searchsorted(day)])
        if day not in ledger.index:
            continue
        assert bucket==ledger.loc[day,'regime'],(day,bucket,ledger.loc[day,'regime'])
        index=tr.index.get_loc(day)
        def vol(sleeve):
            if not sleeve:
                return None
            window=tr.iloc[index-20:index]
            mixed=sum(share*np.diff(np.log(window[ticker].to_numpy(float)))
                      for ticker,share in income_sleeve(sleeve).items())
            return float(np.std(mixed,ddof=1)*np.sqrt(252))
        sleeve=rv.bucket_sleeve(config,bucket,slot,qsig,ssig)
        scale=risk_scale(config,bucket,qsig,slot,vol(sleeve),sleeve)
        levels=None
        if bucket!='STRONG_UP' and rv.derisk_asset(config,bucket)=='CASCADE':
            levels=[rv.vol_target_scale(config,vol(level)) for level in rv.derisk_levels(config,bucket,slot,qsig,ssig)]
        weights=target_weights(config,bucket,qsig,slot,scale,spmo_sig=ssig,level_scales=levels)
        delta=max(abs(weights.get(t,0)-ledger.loc[day,'target_'+t]) for t in TICKERS)
        error=max(error,delta)
        assert delta<1e-10,(day,delta,weights)
        checked+=1; regimes[bucket]=regimes.get(bucket,0)+1
    assert checked==len(ledger)
    result=dict(version=VERSION,comparison='Selected balanced profile: up SCHD15 floor, sideways SCHD20/SGOV80, attack volatility20%, direct SCHD. Target/classifier parity only, not live fills or future returns.',
        first=str(first.date()),last=str(end.date()),sessions=checked,regimes=regimes,max_target_weight_error=error,
        source_files={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [cache,reference,provenance,Path(__file__)]})
    output.write_text(json.dumps(result,indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research-root', type=Path, required=True)
    parser.add_argument('--output', type=Path,
                        default=Path(__file__).resolve().parents[1]/'ADOPTION_CHECK.json')
    args = parser.parse_args()
    print(json.dumps(verify(args.research_root, args.output), indent=2))
