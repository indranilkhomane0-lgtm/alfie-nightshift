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

def fetch_funding_rate(asset: str, days: int = 7) -> float:
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
            return 0.0
        rates = [float(x["fundingRate"]) for x in data]
        val = float(np.mean(rates))
        log.debug("%s funding rate (7d avg): %.5f", asset, val)
        return val
    except Exception as e:
        log.warning("funding_rate failed for %s: %s", asset, e)
        return 0.0

def fetch_oi_trend(asset: str, days: int = 7) -> float:
    """Open interest 7-day % change."""
    try:
        r = requests.get(
            f"{BASE}/futures/data/openInterestHist",
            params={"symbol": _sym(asset), "period": "1d", "limit": days + 1},
            timeout=8
        )
        data = r.json()
        if not data or len(data) < 2 or isinstance(data, dict):
            return 0.0
        oi_start = float(data[0]["sumOpenInterest"])
        oi_end   = float(data[-1]["sumOpenInterest"])
        val = (oi_end - oi_start) / oi_start if oi_start else 0.0
        log.debug("%s OI trend 7d: %.4f", asset, val)
        return val
    except Exception as e:
        log.warning("oi_trend failed for %s: %s", asset, e)
        return 0.0

def fetch_longshort_ratio(asset: str) -> float:
    """Current long/short account ratio (global)."""
    try:
        r = requests.get(
            f"{BASE}/futures/data/globalLongShortAccountRatio",
            params={"symbol": _sym(asset), "period": "1h", "limit": 1},
            timeout=8
        )
        data = r.json()
        if not data or isinstance(data, dict):
            return 0.5
        val = float(data[-1]["longShortRatio"])
        # Normalise to 0–1 (long fraction)
        val = val / (1 + val)
        log.debug("%s long/short ratio: %.3f", asset, val)
        return val
    except Exception as e:
        log.warning("longshort failed for %s: %s", asset, e)
        return 0.5

def fetch_btc_dominance_delta() -> float:
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
        return val
    except Exception as e:
        log.warning("btc_dominance failed: %s", e)
        return 0.0

def get_all_signals(asset: str) -> dict:
    """
    Fetch all derivatives signals for one asset.
    Falls back to neutral values on any failure.
    """
    return {
        "funding_rate":       fetch_funding_rate(asset),
        "oi_trend_7d":        fetch_oi_trend(asset),
        "longshort_ratio":    fetch_longshort_ratio(asset),
        "exchange_flow_7d":   0.0,   # needs Glassnode — add later
        "btc_dominance_delta": fetch_btc_dominance_delta(),
    }
