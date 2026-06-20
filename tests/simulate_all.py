
import sys, numpy as np, pandas as pd
from pathlib import Path
from datetime import date, datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

ok_count = 0
fail_count = 0

def check(label, passed, value=""):
    global ok_count, fail_count
    icon = "✓" if passed else "✗"
    val = f"  ({value})" if value else ""
    print(f"    {icon}  {label}{val}")
    if passed: ok_count += 1
    else: fail_count += 1

print()
print("=" * 56)
print("  ALFIE SIMULATION SUITE")
print("=" * 56)

# DATA
print("\n  [DATA]")
try:
    from nightshift.cycle import _synthetic
    df = _synthetic("BTC/USDT", 500)
    check("Shape is 500 rows", df.shape == (500, 5), df.shape)
    check("No NaN values", df.isnull().sum().sum() == 0)
    check("High >= Low always", (df["high"] < df["low"]).sum() == 0)
    check("Returns within 30 pct", (df["close"].pct_change().abs() > 0.3).sum() == 0)
except Exception as e:
    check("Data layer", False, str(e))

try:
    from nightshift.derivatives import get_all_signals
    sig = get_all_signals("BTC/USDT")
    keys = ["funding_rate", "longshort_ratio", "oi_trend_7d"]
    check("All derivative keys present", all(k in sig for k in keys))
    check("Funding rate in range", -0.01 < sig["funding_rate"] < 0.01, round(sig["funding_rate"], 6))
    check("Long short ratio in range", 0 < sig["longshort_ratio"] < 1, round(sig["longshort_ratio"], 3))
except Exception as e:
    check("Derivatives", False, str(e))

# REGIME
print("\n  [REGIME]")
try:
    from nightshift.regime_engine import RegimeEngine, compute_hmm_features
    p = _synthetic("BTC/USDT", 500)
    f = compute_hmm_features(p)
    e = RegimeEngine()
    e.fit(f)
    r = e.classify(f)
    check("HMM fits without error", True)
    check("State is 0 to 3", r.state in {0, 1, 2, 3}, f"{r.state} = {r.name}")
    check("Probability 0 to 1", 0 <= r.probability <= 1, round(r.probability, 3))
    check("Feature matrix no NaN", f.isnull().sum().sum() == 0)
except Exception as e:
    check("Regime engine", False, str(e))

# WFO
print("\n  [WFO ENGINE]")
try:
    from nightshift.wfo_engine import gt_score, sharpe
    np.random.seed(42)
    noise_s = gt_score(np.random.normal(0, 0.02, 252))
    perf_s  = gt_score(np.full(252, 0.001))
    zero_s  = sharpe(np.zeros(100))
    check("GT-Score on noise is near zero", -0.5 < noise_s < 0.5, round(noise_s, 3))
    check("GT-Score on perfect is above 1", perf_s > 1.0, round(perf_s, 3))
    check("Sharpe of zeros is -999", zero_s == -999.0, zero_s)
except Exception as e:
    check("WFO engine", False, str(e))

# MC GATES
print("\n  [MC GATES]")
try:
    from nightshift.mc_gate import gate1_shuffle, gate2_noise, gate3_slippage, gate4_blocks
    np.random.seed(42)
    perfect = np.full(252, 0.002)
    noise   = np.random.normal(0, 0.02, 252)
    g1p = gate1_shuffle(perfect, n=200)
    g1n = gate1_shuffle(noise,   n=200)
    g2  = gate2_noise(perfect,   n=200)
    np.random.seed(7)
    g3  = gate3_slippage(np.abs(np.random.normal(0.003, 0.005, 252)), n=200)
    g4  = gate4_blocks(perfect,  n=200)
    check("Gate 1 perfect strategy passes", g1p > 0.3, round(g1p, 3))
    check("Gate 1 noise strategy fails",    g1n < 0.3, round(g1n, 3))
    check("Gate 2 survives noise",          g2  > 0.0, round(g2,  3))
    check("Gate 3 survives 3x slippage",    g3  >= 0,  round(g3,  3))
    check("Gate 4 block randomisation",     g4  > 0,   round(g4,  3))
except Exception as e:
    check("MC gates", False, str(e))

# META-MODEL
print("\n  [META-MODEL]")
try:
    from nightshift.config import DB_PATH
    from nightshift.db import corpus_size
    from nightshift.meta_model import MetaModel
    check("DB file exists", DB_PATH.exists(), DB_PATH.name)
    check("Corpus size non-negative", corpus_size() >= 0, corpus_size())
    MetaModel()
    check("MetaModel initialises", True)
except Exception as e:
    check("Meta-model", False, str(e))

# BRIEF
print("\n  [BRIEF]")
try:
    from nightshift.config import BRIEF_DIR
    today = date.today().strftime("%Y%m%d")
    bf = BRIEF_DIR / f"brief_{today}.txt"
    check("Brief file exists today", bf.exists(), bf.name)
    if bf.exists():
        txt = bf.read_text()
        needed  = ["REGIME", "META-MODEL", "TOP CONFIGURATIONS", "LIVE HEALTH"]
        missing = [s for s in needed if s not in txt]
        check("All sections present", len(missing) == 0, f"missing={missing}")
        check("Brief longer than 200 chars", len(txt) > 200, len(txt))
except Exception as e:
    check("Brief", False, str(e))

# SUMMARY
total = ok_count + fail_count
status = "ALL GREEN" if fail_count == 0 else f"{fail_count} FAILED"
print()
print("=" * 56)
print(f"  {ok_count}/{total} passed  |  {status}")
print("=" * 56)
print()
sys.exit(0 if fail_count == 0 else 1)
