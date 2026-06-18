import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional
import numpy as np
from nightshift.config import (DECAY_RATIO_WARN, DECAY_RATIO_SUSPEND,
    DECAY_CONFIRM_DAYS, DD_EARLY_STOP_FRAC)
from nightshift.db import insert_monitor_check, get_monitor_history
from nightshift.wfo_engine import sharpe

log = logging.getLogger(__name__)

@dataclass
class MonitorStatus:
    config_id: str; status: str
    live_sharpe_rolling: float; predicted_sharpe: float
    decay_ratio: float; live_max_dd: float; wfo_max_dd: float
    dd_fraction: float; consecutive_warn: int; message: str

    @property
    def should_suspend(self): return self.status in ("suspend","dd_stop")
    @property
    def emoji(self): return {"healthy":"✓","warn":"⚠","suspend":"✗","dd_stop":"🛑"}.get(self.status,"?")

class LiveMonitor:
    def __init__(self): self._active = {}

    def register(self, config_id, wfo_sharpe, wfo_max_dd, cycle_id, deployment_date):
        self._active[config_id] = {"wfo_sharpe":wfo_sharpe,"wfo_max_dd":wfo_max_dd,
            "cycle_id":cycle_id,"deployment_date":deployment_date,"live_returns":[]}
        log.info("Monitor registered: %s (pred Sharpe=%.2f)", config_id, wfo_sharpe)

    def add_return(self, config_id, daily_return):
        if config_id in self._active:
            self._active[config_id]["live_returns"].append(daily_return)

    def check(self, config_id):
        if config_id not in self._active: return None
        rec  = self._active[config_id]
        rets = np.array(rec["live_returns"])
        if len(rets) < 5:
            return MonitorStatus(config_id=config_id, status="healthy",
                live_sharpe_rolling=0.0, predicted_sharpe=rec["wfo_sharpe"],
                decay_ratio=1.0, live_max_dd=0.0, wfo_max_dd=rec["wfo_max_dd"],
                dd_fraction=0.0, consecutive_warn=0, message="Insufficient live data")
        w20      = rets[-20:]
        ls       = sharpe(w20)
        pred     = rec["wfo_sharpe"]
        decay    = ls / pred if pred else 1.0
        eq       = np.cumprod(1 + rets)
        peak     = np.maximum.accumulate(eq)
        lmdd     = float(((eq-peak)/peak).min())
        wmdd     = rec["wfo_max_dd"]
        ddfrac   = lmdd / wmdd if wmdd else 0.0
        hist     = get_monitor_history(config_id, DECAY_CONFIRM_DAYS)
        cw       = sum(1 for h in hist if h["status"] in ("warn","suspend"))
        if ddfrac <= -DD_EARLY_STOP_FRAC:
            status = "dd_stop"
            msg    = f"DD early-stop: {lmdd:.1%} = {ddfrac:.0%} of WFO limit"
        elif decay < DECAY_RATIO_SUSPEND and cw >= DECAY_CONFIRM_DAYS-1:
            status = "suspend"
            msg    = f"Sustained decay: live={ls:.2f} pred={pred:.2f} ratio={decay:.2f}"
        elif decay < DECAY_RATIO_WARN:
            status = "warn"; cw += 1
            msg    = f"Decay warn: live={ls:.2f} pred={pred:.2f} [{cw}/{DECAY_CONFIRM_DAYS}]"
        else:
            status = "healthy"; cw = 0
            msg    = f"Healthy: live={ls:.2f} pred={pred:.2f} ratio={decay:.2f}"
        ms = MonitorStatus(config_id=config_id, status=status,
            live_sharpe_rolling=ls, predicted_sharpe=pred, decay_ratio=decay,
            live_max_dd=lmdd, wfo_max_dd=wmdd, dd_fraction=ddfrac,
            consecutive_warn=cw, message=msg)
        insert_monitor_check({"config_id":config_id,"cycle_id":rec["cycle_id"],
            "check_date":str(date.today()),"live_sharpe_rolling":ls,
            "predicted_sharpe":pred,"decay_ratio":decay,"live_max_dd":lmdd,
            "wfo_max_dd":wmdd,"dd_fraction":ddfrac,"status":status,"consecutive_warn":cw})
        log.info("Monitor %s %s: %s", ms.emoji, config_id, msg)
        return ms

    def check_all(self): return [self.check(c) for c in list(self._active)]
    def deregister(self, config_id): self._active.pop(config_id, None)
    @property
    def active_configs(self): return list(self._active)
    def live_performance_snapshot(self):
        return {cid: sharpe(np.array(r["live_returns"][-20:]))
                for cid,r in self._active.items() if len(r["live_returns"])>=5}
