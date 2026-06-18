import logging
from dataclasses import dataclass
import numpy as np
from nightshift.config import (MC_SIMS, MC_NOISE_SIGMA, MC_SLIPPAGE_MIN,
    MC_SLIPPAGE_MAX, MC_PARAM_PERTURB, MC_BLOCK_N,
    MC_GATE1_MIN_P10_SHARPE, MC_GATE2_MIN_P10_SHARPE,
    MC_GATE3_MIN_P10_SHARPE, MC_GATE4_MIN_P10_SHARPE,
    MC_GATE5_MAX_SENSITIVITY)
from nightshift.wfo_engine import sharpe

RNG = np.random.default_rng(42)
log = logging.getLogger(__name__)

@dataclass
class MCGateResult:
    gate1_p10: float; gate2_p10: float; gate3_p10: float
    gate4_p10: float; gate5_sensitivity: float
    gate1_pass: bool; gate2_pass: bool; gate3_pass: bool
    gate4_pass: bool; gate5_pass: bool

    @property
    def all_pass(self):
        return all([self.gate1_pass, self.gate2_pass,
                    self.gate3_pass, self.gate4_pass, self.gate5_pass])

    @property
    def composite(self):
        return float(0.25*max(self.gate1_p10,0) + 0.20*max(self.gate2_p10,0) +
                     0.20*max(self.gate3_p10,0) + 0.20*max(self.gate4_p10,0) +
                     0.15*max(1.0-self.gate5_sensitivity,0))

    def summary(self):
        s = "PASS" if self.all_pass else "FAIL"
        return (f"[{s}] G1={self.gate1_p10:.2f} G2={self.gate2_p10:.2f} "
                f"G3={self.gate3_p10:.2f} G4={self.gate4_p10:.2f} "
                f"G5_sens={self.gate5_sensitivity:.2f} comp={self.composite:.2f}")

def _p10(sims):
    vals = [sharpe(r) for r in sims if len(r) > 2]
    return float(np.percentile(vals, 10)) if vals else -999.0

def gate1_shuffle(r, n=MC_SIMS):
    return _p10([RNG.permutation(r) for _ in range(n)])

def gate2_noise(r, n=MC_SIMS):
    return _p10([r + RNG.normal(0, MC_NOISE_SIGMA, len(r)) for _ in range(n)])

def gate3_slippage(r, n=MC_SIMS):
    def slip(ret, m): return ret - (ret != 0).astype(float) * 0.0005 * m
    return _p10([slip(r, RNG.uniform(MC_SLIPPAGE_MIN, MC_SLIPPAGE_MAX))
                 for _ in range(n)])

def gate4_blocks(r, n=MC_SIMS):
    sz   = len(r)
    bl   = sz // MC_BLOCK_N
    blks = [r[i*bl:(i+1)*bl] for i in range(MC_BLOCK_N)]
    if sz % MC_BLOCK_N:
        blks[-1] = np.concatenate([blks[-1], r[MC_BLOCK_N*bl:]])
    return _p10([np.concatenate([blks[i] for i in RNG.permutation(len(blks))])
                 for _ in range(n)])

def gate5_sensitivity(strategy_fn, prices, params):
    base_r  = strategy_fn(prices, params)
    base_sh = sharpe(base_r) if len(base_r) > 10 else 0.0
    if base_sh <= 0: return 1.0
    max_drop = 0.0
    for key, val in params.items():
        if not isinstance(val, (int, float)): continue
        for sign in (+1, -1):
            p2 = dict(params)
            p2[key] = (val + sign*max(1,int(abs(val)*MC_PARAM_PERTURB))
                       if isinstance(val, int)
                       else val*(1+sign*MC_PARAM_PERTURB))
            try:
                sh2  = sharpe(strategy_fn(prices, p2))
                drop = (base_sh - sh2) / abs(base_sh)
                max_drop = max(max_drop, drop)
            except: max_drop = max(max_drop, 1.0)
    return float(min(max_drop, 1.0))

def run_mc_gates(result, strategy_fn, prices, n_sims=MC_SIMS):
    oos = result.oos_returns
    g1  = gate1_shuffle(oos, n_sims)
    g2  = gate2_noise(oos, n_sims)
    g3  = gate3_slippage(oos, n_sims)
    g4  = gate4_blocks(oos, n_sims)
    g5  = gate5_sensitivity(strategy_fn, prices, result.params)
    mc  = MCGateResult(
        gate1_p10=g1, gate2_p10=g2, gate3_p10=g3, gate4_p10=g4,
        gate5_sensitivity=g5,
        gate1_pass=g1>=MC_GATE1_MIN_P10_SHARPE,
        gate2_pass=g2>=MC_GATE2_MIN_P10_SHARPE,
        gate3_pass=g3>=MC_GATE3_MIN_P10_SHARPE,
        gate4_pass=g4>=MC_GATE4_MIN_P10_SHARPE,
        gate5_pass=g5<=MC_GATE5_MAX_SENSITIVITY)
    result.mc_results = {"gate1_p10":g1,"gate2_p10":g2,"gate3_p10":g3,
                         "gate4_p10":g4,"gate5_sensitivity":g5,"composite":mc.composite}
    result.mc_passed  = mc.all_pass
    log.info("MC %s: %s", result.config_id, mc.summary())
    return mc
