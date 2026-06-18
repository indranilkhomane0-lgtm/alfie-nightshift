#!/usr/bin/env python3
"""
run_nightshift.py — Alfie Night Shift runner

  python run_nightshift.py          # full cycle with real Binance data
  python run_nightshift.py --demo   # seed corpus + run cycle (test everything)
  python run_nightshift.py --health # corpus health report
  python run_nightshift.py --features # feature importances
"""
import argparse, logging, sys
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"nightshift/logs/cycle_{date.today()}.log"),
    ],
)
log = logging.getLogger("run_nightshift")

def cmd_demo(args):
    import numpy as np
    from nightshift.db import init_db, insert_config_entry, update_live_outcome
    from nightshift.meta_model import get_meta_model
    from nightshift.cycle import NightShiftCycle
    init_db()
    log.info("Seeding corpus with 60 synthetic outcomes …")
    rng = np.random.default_rng(42)
    for i in range(60):
        wfo_sh = float(rng.uniform(0.5,2.5))
        regime = int(rng.choice([0,1,2,3],p=[0.45,0.25,0.20,0.10]))
        g1=float(rng.uniform(0.1,0.9)); g2=float(rng.uniform(0.1,0.8))
        g3=float(rng.uniform(-0.1,0.7)); g4=float(rng.uniform(0.1,0.8))
        g5=float(rng.uniform(0.0,0.5)); mc=(g1+g2+g3+g4+(1-g5))/5
        logit = (wfo_sh-1.2)+mc-0.3*regime
        surv  = bool(rng.random() < 1/(1+np.exp(-logit)))
        decay = float(rng.beta(3,2)*(1.2 if surv else 0.5))
        dep   = f"2025-{(i//30)+1:02d}-{(i%30)+1:02d}"
        cfg   = {"config_id":f"DEMO_{i:04d}","asset":"BTC/USDT",
                 "strategy_family":str(rng.choice(["momentum","mean_rev","relative"])),
                 "deployment_date":dep,
                 "wfo_sharpe":wfo_sh,"wfo_sortino":wfo_sh*1.2,"wfo_calmar":wfo_sh*0.8,
                 "wfo_max_dd":float(-rng.uniform(0.05,0.20)),
                 "wfo_win_rate":float(rng.uniform(0.40,0.65)),
                 "wfo_gt_score":wfo_sh*0.85,"wfo_n_trades":int(rng.integers(20,80)),
                 "mc_gate1_p10":g1,"mc_gate2_p10":g2,"mc_gate3_p10":g3,
                 "mc_gate4_p10":g4,"mc_gate5_sensitivity":g5,"mc_composite":mc,
                 "regime_state":regime,"regime_prob":float(rng.uniform(0.6,0.95)),
                 "regime_days_in":int(rng.integers(3,30)),
                 "regime_transition_p":float(rng.uniform(0.05,0.20)),
                 "funding_rate":float(rng.normal(0.01,0.04)),
                 "oi_trend_7d":float(rng.normal(0.0,0.08)),
                 "exchange_flow_7d":float(rng.normal(0.0,0.05)),
                 "vol_ratio":float(rng.uniform(0.7,1.5)),
                 "longshort_ratio":float(rng.uniform(0.4,0.7)),
                 "btc_dominance_delta":float(rng.normal(0.0,0.01)),"params":{}}
        insert_config_entry(0, cfg)
        update_live_outcome(cfg["config_id"],dep,wfo_sh*decay,
                            cfg["wfo_max_dd"]*float(rng.uniform(0.5,1.2)),
                            cfg["wfo_n_trades"],decay,surv,"demo seed")
    log.info("Training meta-model …")
    mm = get_meta_model(); ok = mm.train(force=True)
    if ok:
        log.info("Trained — AUC=%.3f  R²=%.3f", mm._cv_clf, mm._cv_reg)
        print("\n── Feature importances ─────────────────────────────────────────")
        for f,v in mm.top_features(10):
            print(f"  {f:35s}  {v:.4f}  {'█'*int(v*60)}")
    log.info("\nRunning full cycle …")
    brief = Path(NightShiftCycle().run()).read_text()
    print("\n" + brief)

def cmd_full(args):
    from nightshift.db import init_db
    from nightshift.cycle import NightShiftCycle
    init_db()
    brief = Path(NightShiftCycle().run()).read_text()
    print("\n" + brief)

def cmd_health(args):
    from nightshift.meta_model import get_meta_model
    h = get_meta_model().corpus_health()
    print("\n── CORPUS HEALTH ───────────────────────────────────────────────")
    for k,v in h.items():
        print(f"  {k:30s}: {v:.4f}" if isinstance(v,float) else f"  {k:30s}: {v}")

def cmd_features(args):
    from nightshift.meta_model import get_meta_model
    mm = get_meta_model()
    if not mm._trained: print("Model not trained yet. Run --demo first."); return
    print("\n── FEATURE IMPORTANCES ─────────────────────────────────────────")
    for f,v in mm.top_features(22):
        print(f"  {f:35s}  {v:.4f}  {'█'*int(v*60)}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--demo",     action="store_true")
    g.add_argument("--health",   action="store_true")
    g.add_argument("--features", action="store_true")
    args = p.parse_args()
    if args.demo:     cmd_demo(args)
    elif args.health: cmd_health(args)
    elif args.features: cmd_features(args)
    else: cmd_full(args)
