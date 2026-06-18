from pathlib import Path

ROOT        = Path(__file__).parent
DB_PATH     = ROOT / "corpus.db"
LOG_DIR     = ROOT / "logs"
BRIEF_DIR   = ROOT / "briefs"

for d in (LOG_DIR, BRIEF_DIR):
    d.mkdir(parents=True, exist_ok=True)

ASSETS          = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
EXCHANGE_ID     = "binance"
OHLCV_DAYS      = 500

N_REGIMES           = 4
REGIME_TRAIN_DAYS   = 252
HMM_ITER            = 500
REGIME_SEED         = 42

REGIME_NAMES = {
    0: "low-vol bull",
    1: "high-vol bull",
    2: "choppy / ranging",
    3: "crisis / bear",
}

REGIME_FAMILY_GATE = {
    0: {"momentum": 1, "mean_rev": 0, "breakout": 1, "relative": 1},
    1: {"momentum": 1, "mean_rev": 1, "breakout": 0, "relative": 1},
    2: {"momentum": 0, "mean_rev": 1, "breakout": 0, "relative": 0},
    3: {"momentum": 0, "mean_rev": 0, "breakout": 0, "relative": 0},
}

REGIME_SIZE_MULT   = {0: 1.0, 1: 0.75, 2: 0.50, 3: 0.20}
REGIME_VOL_TARGET  = {0: 0.14, 1: 0.10, 2: 0.08, 3: 0.04}

WFO_MIN_TRAIN_DAYS  = 180
WFO_TEST_DAYS       = 28
WFO_STEP_DAYS       = 14
OPTUNA_TRIALS       = 200
MIN_OOS_SHARPE      = 0.15

GT_W_SHARPE      = 0.30
GT_W_SIG         = 0.25
GT_W_CONSISTENCY = 0.20
GT_W_SORTINO     = 0.15
GT_W_CALMAR      = 0.10

MC_SIMS                  = 500
MC_NOISE_SIGMA           = 0.001
MC_SLIPPAGE_MIN          = 1.0
MC_SLIPPAGE_MAX          = 3.0
MC_PARAM_PERTURB         = 0.10
MC_BLOCK_N               = 4
MC_GATE1_MIN_P10_SHARPE  = 0.05
MC_GATE2_MIN_P10_SHARPE  = 0.03
MC_GATE3_MIN_P10_SHARPE  = 0.00
MC_GATE4_MIN_P10_SHARPE  = 0.08
MC_GATE5_MAX_SENSITIVITY = 0.70

META_MIN_SAMPLES       = 30
META_SURVIVE_THRESHOLD = 0.50
META_LIVE_WINDOW       = 20
META_RETRAIN_EVERY     = 7
META_N_ESTIMATORS      = 200
META_MAX_DEPTH         = 4
META_LEARNING_RATE     = 0.05
META_SEED              = 99

DECAY_RATIO_WARN    = 0.65
DECAY_RATIO_SUSPEND = 0.50
DECAY_CONFIRM_DAYS  = 3
DD_EARLY_STOP_FRAC  = 0.60

KELLY_FRACTION = 0.25
