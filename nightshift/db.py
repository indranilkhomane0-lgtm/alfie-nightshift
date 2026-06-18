import sqlite3, json, logging
from datetime import datetime, date
from pathlib import Path
from nightshift.config import DB_PATH

log = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS corpus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER, config_id TEXT, asset TEXT,
    strategy_family TEXT, deployment_date TEXT, outcome_date TEXT,
    wfo_sharpe REAL, wfo_sortino REAL, wfo_calmar REAL,
    wfo_max_dd REAL, wfo_win_rate REAL, wfo_gt_score REAL, wfo_n_trades INTEGER,
    mc_gate1_p10 REAL, mc_gate2_p10 REAL, mc_gate3_p10 REAL,
    mc_gate4_p10 REAL, mc_gate5_sensitivity REAL, mc_composite REAL,
    regime_state INTEGER, regime_prob REAL, regime_days_in INTEGER,
    regime_transition_p REAL, funding_rate REAL, oi_trend_7d REAL,
    exchange_flow_7d REAL, vol_ratio REAL, longshort_ratio REAL,
    btc_dominance_delta REAL, params_json TEXT,
    live_sharpe_20d REAL, live_max_dd_20d REAL, live_n_trades INTEGER,
    decay_ratio REAL, survived INTEGER, outcome_note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id INTEGER PRIMARY KEY, cycle_date TEXT,
    regime_state INTEGER, regime_name TEXT,
    n_configs_tested INTEGER, n_mc_passed INTEGER,
    portfolio_action TEXT, brief_path TEXT,
    duration_secs REAL, status TEXT DEFAULT 'running', error_msg TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS live_monitor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id TEXT, cycle_id INTEGER, check_date TEXT,
    live_sharpe_rolling REAL, predicted_sharpe REAL, decay_ratio REAL,
    live_max_dd REAL, wfo_max_dd REAL, dd_fraction REAL,
    status TEXT DEFAULT 'healthy', consecutive_warn INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_corpus_survived ON corpus(survived);
CREATE INDEX IF NOT EXISTS idx_corpus_regime ON corpus(regime_state);
"""

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as c: c.executescript(DDL)
    log.info("DB ready at %s", DB_PATH)

def insert_config_entry(cycle_id, config):
    sql = """INSERT INTO corpus (
        cycle_id,config_id,asset,strategy_family,deployment_date,
        wfo_sharpe,wfo_sortino,wfo_calmar,wfo_max_dd,wfo_win_rate,
        wfo_gt_score,wfo_n_trades,mc_gate1_p10,mc_gate2_p10,mc_gate3_p10,
        mc_gate4_p10,mc_gate5_sensitivity,mc_composite,regime_state,
        regime_prob,regime_days_in,regime_transition_p,funding_rate,
        oi_trend_7d,exchange_flow_7d,vol_ratio,longshort_ratio,
        btc_dominance_delta,params_json
    ) VALUES (
        :cycle_id,:config_id,:asset,:strategy_family,:deployment_date,
        :wfo_sharpe,:wfo_sortino,:wfo_calmar,:wfo_max_dd,:wfo_win_rate,
        :wfo_gt_score,:wfo_n_trades,:mc_gate1_p10,:mc_gate2_p10,:mc_gate3_p10,
        :mc_gate4_p10,:mc_gate5_sensitivity,:mc_composite,:regime_state,
        :regime_prob,:regime_days_in,:regime_transition_p,:funding_rate,
        :oi_trend_7d,:exchange_flow_7d,:vol_ratio,:longshort_ratio,
        :btc_dominance_delta,:params_json
    )"""
    row = {**config, "params_json": json.dumps(config.get("params", {})),
           "cycle_id": cycle_id}
    with get_conn() as c: return c.execute(sql, row).lastrowid

def update_live_outcome(config_id, deployment_date, live_sharpe,
                        live_max_dd, live_n_trades, decay_ratio, survived, note=""):
    sql = """UPDATE corpus SET live_sharpe_20d=:ls,live_max_dd_20d=:ldd,
        live_n_trades=:nt,decay_ratio=:dr,survived=:sv,outcome_note=:note,
        outcome_date=:today,updated_at=datetime('now')
        WHERE config_id=:cid AND deployment_date=:dep"""
    with get_conn() as c:
        c.execute(sql, {"ls":live_sharpe,"ldd":live_max_dd,"nt":live_n_trades,
                        "dr":decay_ratio,"sv":int(survived),"note":note,
                        "today":str(date.today()),"cid":config_id,"dep":deployment_date})

def get_labelled_corpus():
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM corpus WHERE survived IS NOT NULL ORDER BY deployment_date").fetchall()]

def get_pending_outcomes(cutoff_date):
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM corpus WHERE survived IS NULL AND deployment_date<=?",
            (cutoff_date,)).fetchall()]

def corpus_size():
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) FROM corpus WHERE survived IS NOT NULL").fetchone()[0]

def regime_corpus_size(regime):
    with get_conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM corpus WHERE survived IS NOT NULL AND regime_state=?",
            (regime,)).fetchone()[0]

def log_cycle_start(cycle_id, cycle_date):
    with get_conn() as c:
        c.execute("INSERT OR REPLACE INTO cycles(cycle_id,cycle_date,status) VALUES(?,?,'running')",
                  (cycle_id, cycle_date))

def log_cycle_end(cycle_id, *, regime_state, regime_name, n_tested,
                  n_passed, action, brief_path, duration):
    with get_conn() as c:
        c.execute("""UPDATE cycles SET regime_state=?,regime_name=?,
            n_configs_tested=?,n_mc_passed=?,portfolio_action=?,brief_path=?,
            duration_secs=?,status='complete' WHERE cycle_id=?""",
            (regime_state,regime_name,n_tested,n_passed,action,brief_path,duration,cycle_id))

def log_cycle_error(cycle_id, msg):
    with get_conn() as c:
        c.execute("UPDATE cycles SET status='error',error_msg=? WHERE cycle_id=?",
                  (msg, cycle_id))

def insert_monitor_check(entry):
    sql = """INSERT INTO live_monitor(config_id,cycle_id,check_date,
        live_sharpe_rolling,predicted_sharpe,decay_ratio,live_max_dd,
        wfo_max_dd,dd_fraction,status,consecutive_warn)
        VALUES(:config_id,:cycle_id,:check_date,:live_sharpe_rolling,
        :predicted_sharpe,:decay_ratio,:live_max_dd,:wfo_max_dd,
        :dd_fraction,:status,:consecutive_warn)"""
    with get_conn() as c: c.execute(sql, entry)

def get_monitor_history(config_id, last_n=5):
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM live_monitor WHERE config_id=? ORDER BY check_date DESC LIMIT ?",
            (config_id, last_n)).fetchall()]
