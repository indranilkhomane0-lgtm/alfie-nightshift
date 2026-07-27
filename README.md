# Alfie Night Shift

An autonomous crypto signal pipeline that publishes its own track record —
wins, losses, no-calls, and failed nights — to a tamper-evident hash chain.

**The product is not the signals. The product is the record.**
You do not have to take anything here on faith. You can verify it yourself,
with one command and no dependencies.

## Verify the record yourself

    git clone https://github.com/indranilkhomane0-lgtm/alfie-nightshift
    cd alfie-nightshift
    python3 nightshift/verify_chain.py

This recomputes every hash from genesis using Python's standard library only.
If any historical entry was edited, deleted, or reordered, it fails at that
exact line and tells you which one.

## How the record works

Each night's brief is appended to reports/chain.jsonl as:

    entry_hash = sha256(prev_hash + canonical_json(payload))

Genesis is 64 zeros. Because each entry commits to the one before it,
editing any past entry breaks every hash after it. There is no filtering
hook in the publisher — losses are written by the same code path as wins.

When a nightly run fails, a PIPELINE_FAILURE entry is published instead of
nothing. A gap in the record would be suspicious; a published failure is
honest.

## Predictions are stamped before the market moves

nightshift/stamp_prediction.py writes a concrete, gradeable claim to
reports/predictions.jsonl on the night it is made: asset, entry price,
direction, and a settle date 7 days out.

Every strategy in this repo is long-only. The stamper can therefore emit only
"long" (the strategy's entry condition is true today) or "none" (no entry
signal). It never emits "short". The record must not claim a call the system
never made.

## Outcomes are graded by a fixed rule

nightshift/label_outcomes.py runs daily. When a settle date arrives, it
fetches that day's daily close from Binance — the same source as the entry
price — and grades against a rule declared in advance and never changed
retroactively:

    long -> WIN if settle_close > entry_price, else LOSS
    none -> NO_CALL (excluded from the corpus entirely)

The labeled outcome is then chained. NO_CALL exists so the record never
takes credit for a call it did not make.

## The nightly pipeline

Seven stages, run against BTC/USDT, ETH/USDT, and SOL/USDT on Binance:

1. Ingest — 500 days of OHLCV plus derivatives signals
2. Collect pending outcomes — pull results from live-monitored configs
3. Regime classification — 4-state HMM (low-vol bull, high-vol bull,
   choppy/ranging, crisis/bear); each regime gates which strategy families
   are eligible to run at all
4. Walk-forward optimisation — out-of-sample parameter search per family
   per asset, with a minimum OOS Sharpe floor
5. Monte Carlo gating — 5 gates covering noise, slippage, parameter
   perturbation, and block resampling; a config must pass all five
6. Meta-model ranking — survival model ranks the survivors
7. Live monitor + brief — decay checks, auto-suspend, then the brief is
   written and chained

## Honest limitations

- Paper only. No live capital has ever been deployed. Nothing here is a
  claim about realised returns.
- The record is weeks old, not years. Any track record this short is
  statistically weak. That is the point of publishing it from day one
  rather than after it looks good.
- The meta-model is in fallback mode. It requires 30 graded outcomes before
  it trains on real data. It does not have them yet. Until then its ranking
  is a heuristic, not a learned model, and the repo says so.
- Long-only. No shorting, in any market condition.
- One laptop, one person. No redundancy. Missed nights have happened and are
  published as PIPELINE_FAILURE entries.

## Current state

As of 2026-07-28: 10 chain entries, 2 open predictions, 0 graded outcomes
(0 of 30 required for meta-model graduation).

Do not trust this paragraph — it goes stale. Run verify_chain.py for the
live count.

## Layout

    nightshift/cycle.py             the 7-stage nightly pipeline
    nightshift/stamp_prediction.py  writes the gradeable claim
    nightshift/label_outcomes.py    grades it 7 days later
    nightshift/publish_chain.py     appends to the hash chain
    nightshift/verify_chain.py      recomputes it from genesis
    reports/chain.jsonl             the record
    reports/predictions.jsonl       open and settled predictions

## Run it

    python3 run_nightshift.py             # full cycle, live Binance data
    python3 nightshift/label_outcomes.py  # grade anything that has settled
    python3 nightshift/verify_chain.py    # check the chain
