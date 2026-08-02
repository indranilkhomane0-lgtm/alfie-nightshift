import numpy as np
import pandas as pd
import optuna

def _safe_ret(prices, signal):
    close   = prices["close"].values
    ret_raw = np.diff(close) / close[:-1]
    sig     = signal.values
    sig_lag = np.roll(sig,1); sig_lag[0]=0.0
    trade_r = sig_lag[1:] * ret_raw
    changes = np.abs(np.diff(sig_lag[1:]))
    cost    = np.concatenate([[0.0], changes*0.0005])[:len(trade_r)]
    return np.concatenate([[0.0], trade_r-cost])

def momentum_strategy(prices, params):
    close = prices["close"]
    fast  = int(params.get("fast_ma",18))
    slow  = int(params.get("slow_ma",72))
    rsi_e = int(params.get("rsi_entry",38))
    if slow > len(prices)-10 or fast >= slow: return np.zeros(len(prices))
    ma_f  = close.rolling(fast).mean()
    ma_s  = close.rolling(slow).mean()
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi   = 100 - 100/(1+(gain/loss.replace(0,np.nan)))
    sig   = pd.Series(0.0, index=prices.index)
    sig[(ma_f>ma_s)&(rsi>rsi_e)] = 1.0
    return _safe_ret(prices, sig)

def momentum_params(trial):
    fast = trial.suggest_int("fast_ma",5,50)
    return {"fast_ma":fast,
            "slow_ma":trial.suggest_int("slow_ma",fast+20,200),
            "rsi_entry":trial.suggest_int("rsi_entry",20,55)}

def mean_rev_strategy(prices, params):
    close   = prices["close"]
    rl      = int(params.get("rsi_low",28))
    rh      = int(params.get("rsi_high",72))
    rp      = int(params.get("rsi_period",14))
    mh      = int(params.get("max_hold_days",5))
    if rp > len(prices)-5: return np.zeros(len(prices))
    delta   = close.diff()
    gain    = delta.clip(lower=0).rolling(rp).mean()
    loss    = (-delta.clip(upper=0)).rolling(rp).mean()
    rsi     = 100 - 100/(1+(gain/loss.replace(0,np.nan)))
    sig     = pd.Series(0.0, index=prices.index)
    hold    = 0; in_t = False
    for i in range(1,len(prices)):
        r = rsi.iloc[i]
        if np.isnan(r): continue
        if not in_t and r < rl: sig.iloc[i]=1.0; in_t=True; hold=mh
        elif in_t:
            hold -= 1
            if r > rh or hold <= 0: sig.iloc[i]=0.0; in_t=False
            else: sig.iloc[i]=1.0
    return _safe_ret(prices, sig)

def mean_rev_params(trial):
    rl = trial.suggest_int("rsi_low",15,40)
    rh = trial.suggest_int("rsi_high",60,85)
    return {"rsi_low":rl,"rsi_high":rh,
            "rsi_period":trial.suggest_int("rsi_period",7,21),
            "max_hold_days":trial.suggest_int("max_hold_days",2,10)}

def relative_strategy(prices, params):
    close = prices["close"] if "close" in prices.columns else prices.iloc[:,0]
    lb    = int(params.get("lookback",14))
    ez    = float(params.get("entry_z",1.5))
    xz    = float(params.get("exit_z",0.3))
    mh    = int(params.get("max_hold_days",7))
    if len(close)<lb+10: return np.zeros(len(close))
    ret   = close.pct_change(lb).fillna(0)
    mu    = ret.rolling(30).mean()
    sigma = ret.rolling(30).std().replace(0,np.nan)
    z     = (ret-mu)/sigma
    sig   = pd.Series(0.0, index=close.index)
    hold  = 0; in_t = False
    for i in range(30,len(close)):
        zv = z.iloc[i]
        if np.isnan(zv): continue
        if not in_t and zv>ez: sig.iloc[i]=1.0; in_t=True; hold=mh
        elif in_t:
            hold-=1
            if zv<xz or hold<=0: sig.iloc[i]=0.0; in_t=False
            else: sig.iloc[i]=1.0
    return _safe_ret(prices, sig) if "close" in prices.columns else np.zeros(len(prices))

def relative_params(trial):
    return {"lookback":trial.suggest_int("lookback",5,30),
            "entry_z":trial.suggest_float("entry_z",0.8,2.5),
            "exit_z":trial.suggest_float("exit_z",0.0,0.8),
            "max_hold_days":trial.suggest_int("max_hold_days",3,14)}

def breakout_strategy(prices, params):
    """Donchian channel breakout, long-only.

    Enter when close exceeds the highest close of the prior `lookback` days
    (excluding today, so the condition is knowable at the close being acted
    on and never uses its own bar). Exit on a close below the prior
    `exit_lookback`-day low, or after `max_hold_days`, whichever comes
    first.

    Deliberately the textbook Donchian rule and nothing more. This family
    exists because REGIME_FAMILY_GATE has listed "breakout" as eligible in
    regime 0 since the first commit while STRATEGY_REGISTRY never
    implemented it -- a gate/registry mismatch self_audit check 6 has been
    reporting nightly. It is added to close that gap and to raise the share
    of nights with a live entry signal (measured 21.8% across the existing
    three families), NOT because it is expected to be a better strategy.
    Breakouts fire on different days than mean reversion and momentum,
    which is the entire point: more nights with a gradeable signal, without
    loosening any threshold on the families already here.

    Long-only, matching every other family. The stamper cannot emit
    "short".
    """
    close = prices["close"]
    lb    = int(params.get("lookback", 20))
    xlb   = int(params.get("exit_lookback", 10))
    mh    = int(params.get("max_hold_days", 10))
    if lb >= len(prices) - 5 or xlb >= len(prices) - 5:
        return np.zeros(len(prices))

    # shift(1) excludes today's bar from its own channel -- without it the
    # high would include the close being tested and the rule could never
    # trigger correctly. This is the look-ahead trap for this family.
    upper = close.rolling(lb).max().shift(1)
    lower = close.rolling(xlb).min().shift(1)

    sig  = pd.Series(0.0, index=prices.index)
    hold = 0
    in_t = False
    for i in range(1, len(prices)):
        c, u, l = close.iloc[i], upper.iloc[i], lower.iloc[i]
        if np.isnan(u) or np.isnan(l):
            continue
        if not in_t:
            if c > u:
                sig.iloc[i] = 1.0; in_t = True; hold = mh
        else:
            hold -= 1
            if c < l or hold <= 0:
                sig.iloc[i] = 0.0; in_t = False
            else:
                sig.iloc[i] = 1.0
    return _safe_ret(prices, sig)

def breakout_params(trial):
    lb = trial.suggest_int("lookback", 10, 60)
    return {"lookback": lb,
            "exit_lookback": trial.suggest_int("exit_lookback", 5, max(6, lb // 2)),
            "max_hold_days": trial.suggest_int("max_hold_days", 3, 20)}

STRATEGY_REGISTRY = {
    "momentum": {"fn":momentum_strategy,"param_space":momentum_params,"family":"momentum"},
    "mean_rev": {"fn":mean_rev_strategy,"param_space":mean_rev_params,"family":"mean_rev"},
    "relative": {"fn":relative_strategy,"param_space":relative_params,"family":"relative"},
    "breakout": {"fn":breakout_strategy,"param_space":breakout_params,"family":"breakout"},
}
