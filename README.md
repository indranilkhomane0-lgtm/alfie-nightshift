# Alfie Night Shift

Autonomous crypto trading-signal engine for BTC/ETH/SOL (USDT pairs).
Runs unattended every night at 00:00 UTC. Publishes every result — wins,
losses, waits, and pipeline failures — to a public, tamper-evident record
in this repository.

**Paper trading only. No profit claims. The record is the product.**

## Why this repo exists

Most trading signals live on screenshots, and screenshots lie by omission.
Alfie's nightly brief is committed here automatically, every night, before
any human sees it. With the hash chain (`reports/chain.jsonl`), each entry
is hashed against the previous one — editing or deleting any historical
entry breaks every hash after it. Verify in under two minutes, stdlib only:

```bash
python3 nightshift/verify_chain.py
```

`CHAIN VALID` means nothing in the record has been altered since
publication. You are not asked to believe anything.

## The pipeline (7 stages, nightly, ~17s cycle)

1. **Market data ingestion** — BTC/ETH/SOL USDT
2. **HMM regime detection** (`regime_engine.py`) — regime, persistence,
   transition probability; sets size multiplier and vol target
3. **Walk-forward optimization** (`wfo_engine.py`) — Optuna, 21 folds;
   Sharpe / Sortino / MaxDD per configuration
4. **5-gate Monte Carlo screen** (`mc_gate.py`) — G1–G5 robustness gates,
   including parameter sensitivity
5. **Meta-model scoring** (`meta_model.py`) — **currently `[FALLBACK]`:
   corpus has fewer than 30 labeled outcome rows, so ranking uses
   GT-Score. The brief prints this honestly every night until the model
   graduates.**
6. **Nightly brief** (`nightshift/briefs/brief_YYYYMMDD.txt`)
7. **Publication** — committed to this repo by launchd, no human review

## Reading the record

- `nightshift/briefs/` — every nightly brief since inception (30+ and counting)
- `reports/chain.jsonl` — hash-chained record (tamper-evident)
- `docs/index.html` — live track record page
- A missed night is published as a `PIPELINE_FAILURE` entry. Gaps are
  suspicious; failures are honest.

## What this is not

- Not financial advice. Not live-money results. Paper signals only.
- No win-rate marketing while the meta-model is in fallback — no
  performance claims the record can't back.

## Legacy

`alfie/` contains the original Day-1 prototype (single-pass regime +
signal generator). It is retained for history; the live system is
`nightshift/`. `alfie/signals/signal_latest.json` is a legacy artifact
and is not the current record.

## Status

Run `python3 nightshift/verify_chain.py` — chain length, outcome counts,
and integrity should come from the record, not from this README.
