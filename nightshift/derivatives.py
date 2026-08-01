"""
nightshift/derivatives.py
Real derivatives signals from Binance public futures API.
No API key required. All public endpoints.
"""
import logging
import requests
import numpy as np
from datetime import datetime

log = logging.getLogger(__name__)

BASE = "https://fapi.binance.com"

def _sym(asset: str) -> str:
    """BTC/USDT → BTCUSDT"""
    return asset.replace("/", "")

def fetch_funding_rate(asset: str, days: int = 7, status: dict = None) -> float:
    """Average funding rate over last N days (8h periods)."""
    try:
        limit = days * 3  # 3 funding periods per day
        r = requests.get(
            f"{BASE}/fapi/v1/fundingRate",
            params={"symbol": _sym(asset), "limit": limit},
            timeout=8
        )
        data = r.json()
        if not data or isinstance(data, dict):
            if status is not None: status["funding_rate"] = False
            return 0.0
        rates = [float(x["fundingRate"]) for x in data]
        val = float(np.mean(rates))
        log.debug("%s funding rate (7d avg): %.5f", asset, val)
        if status is not None: status["funding_rate"] = True
        return val
    except Exception as e:
        log.warning("funding_rate failed for %s: %s", asset, e)
        if status is not None: status["funding_rate"] = False
        return 0.0

def fetch_oi_trend(asset: str, days: int = 7, status: dict = None) -> float:
    """Open interest 7-day % change."""
    try:
        r = requests.get(
            f"{BASE}/futures/data/openInterestHist",
            params={"symbol": _sym(asset), "period": "1d", "limit": days + 1},
            timeout=8
        )
        data = r.json()
        if not data or len(data) < 2 or isinstance(data, dict):
            if status is not None: status["oi_trend_7d"] = False
            return 0.0
        oi_start = float(data[0]["sumOpenInterest"])
        oi_end   = float(data[-1]["sumOpenInterest"])
        val = (oi_end - oi_start) / oi_start if oi_start else 0.0
        log.debug("%s OI trend 7d: %.4f", asset, val)
        if status is not None: status["oi_trend_7d"] = True
        return val
    except Exception as e:
        log.warning("oi_trend failed for %s: %s", asset, e)
        if status is not None: status["oi_trend_7d"] = False
        return 0.0

def fetch_longshort_ratio(asset: str, status: dict = None) -> float:
    """Current long/short account ratio (global)."""
    try:
        r = requests.get(
            f"{BASE}/futures/data/globalLongShortAccountRatio",
            params={"symbol": _sym(asset), "period": "1h", "limit": 1},
            timeout=8
        )
        data = r.json()
        if not data or isinstance(data, dict):
            if status is not None: status["longshort_ratio"] = False
            return 0.5
        val = float(data[-1]["longShortRatio"])
        # Normalise to 0–1 (long fraction)
        val = val / (1 + val)
        log.debug("%s long/short ratio: %.3f", asset, val)
        if status is not None: status["longshort_ratio"] = True
        return val
    except Exception as e:
        log.warning("longshort failed for %s: %s", asset, e)
        if status is not None: status["longshort_ratio"] = False
        return 0.5

def fetch_btc_dominance_delta(status: dict = None) -> float:
    """BTC dominance 7-day change via CoinGecko free API."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=8
        )
        data = r.json()
        dom = data["data"]["market_cap_percentage"].get("btc", 50.0)
        # We don't have 7-day history here, return current as a signal
        # Normalise: 50% dominance = 0, deviations are meaningful
        val = float((dom - 50.0) / 50.0)
        log.debug("BTC dominance: %.1f%% (normalised: %.3f)", dom, val)
        if status is not None: status["btc_dominance_delta"] = True
        return val
    except Exception as e:
        log.warning("btc_dominance failed: %s", e)
        if status is not None: status["btc_dominance_delta"] = False
        return 0.0

def get_all_signals(asset: str) -> dict:
    """Fetch all derivatives signals for one asset. Falls back to neutral
    values on any failure. See get_all_signals_with_status() for which
    sources actually succeeded vs. fell back."""
    values, _ = get_all_signals_with_status(asset)
    return values

def get_all_signals_with_status(asset: str) -> tuple:
    """Same as get_all_signals(), plus a per-source bool: True if that
    source returned real data, False if it fell back to a default.
    exchange_flow_7d is always False — it's a hardcoded placeholder
    (needs Glassnode), never a real fetch."""
    status = {}
    values = {
        "funding_rate":        fetch_funding_rate(asset, status=status),
        "oi_trend_7d":         fetch_oi_trend(asset, status=status),
        "longshort_ratio":     fetch_longshort_ratio(asset, status=status),
        "exchange_flow_7d":    0.0,   # needs Glassnode — add later
        "btc_dominance_delta": fetch_btc_dominance_delta(status=status),
    }
    status["exchange_flow_7d"] = False
    return values, status
