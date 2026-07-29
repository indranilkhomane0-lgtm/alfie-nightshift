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

Each entry is appended to reports/chain.jsonl as:

    entry_hash = sha256(prev_hash + canonical_json(payload))

Genesis is 64 zeros. Because each entry commits to the one before it,
editing any past entry breaks every hash after it. There is no filtering
hook in the publisher — losses are written by the same code path as wins.

Starting at entry 13, every payload also carries the git HEAD commit SHA
and a flag for whether the working tree was dirty when the entry was
written, and both are inside the hashed payload, not bolted on afterward.
That means a change to how the system produces or grades its record shows
up as a specific commit at a specific entry, instead of an unexplained
shift in behavior partway through the file. If git isn't available at run
time, both fields record "unknown" rather than a guess — the record admits
when it can't identify itself, and still gets written either way. The 12
entries before that don't carry either field; verify_chain.py labels them
"(predates code_version)" rather than guessing a version for them.

The chain currently holds four entry types:

- **NIGHTLY_BRIEF** — the brief produced by that night's pipeline run
  (regime, ranked configs, monitor status), referenced by its own sha256
  rather than embedded in full
- **LABELED_OUTCOME** — a stamped prediction's graded result once its
  settle date arrives: WIN, LOSS, or NO_CALL
- **PIPELINE_FAILURE** — published when a nightly run doesn't complete, so
  a missed night is a recorded fact instead of a silent gap
- **METHODOLOGY_CHANGE** — a short, deliberately-written description of a
  change to how the system produces or grades its own record, published
  manually via `publish_chain.py --methodology "..."` at the moment the
  change is made

## Bitcoin anchoring

Since 2026-07-28, each new chain entry's hash is also timestamped to the
Bitcoin blockchain via the free OpenTimestamps calendar servers
(nightshift/anchor_ots.py, no cost, no API keys). The proof lives next to
the chain as `reports/ots/<entry_hash>.hash.ots`. Entries published before
that date have no proof file and are not anchored — only the hash chain
covers them. As of this writing, 2 of the chain's 13 entries have a proof.
The first entry to reach full Bitcoin confirmation anchored to block
**959944**:

    $ ots info reports/ots/4e010bc8...155a8.hash.ots
    File sha256 hash: 5d1c19cd94501fea70a6eca03b73be0b39ea61540b124fab7fb609c87aba5c0c
    ...
    verify BitcoinBlockHeaderAttestation(959944)
    # Bitcoin block merkle root 167a2db6641020a0f82a28ccce2e4970ee40f90bbf6053f809e3b28fc257e4ed

To confirm that proof actually belongs to this record and not some other
file, hash the proof's input yourself and compare:

    $ shasum -a 256 reports/ots/4e010bc8...155a8.hash
    5d1c19cd94501fea70a6eca03b73be0b39ea61540b124fab7fb609c87aba5c0c  ...

which matches "File sha256 hash" above, and the file's own content is the
entry_hash of a specific line in reports/chain.jsonl.

**The honest limit:** this proves the entry existed no later than block
959944 — it does not prove the entry is older than that, and it says
nothing about entries published after it. Anchoring only bounds one side.
Full verification of a proof also isn't the self-contained one-liner that
verify_chain.py is — `ots verify` needs either a local Bitcoin node or a
block explorer it can query, so it depends on infrastructure outside this
repo. It is a stronger claim than the hash chain alone, not a replacement
for it.

## Predictions are stamped before the market moves

nightshift/stamp_prediction.py writes a concrete, gradeable claim to
reports/predictions.jsonl on the night it is made: asset, entry price,
direction, and a settle date 7 days out.

Every config that passes Monte Carlo gating that night gets its own stamped
prediction — across all three assets, not just the single top-ranked
config — with no selection step between what the system evaluates and what
enters the record. An asset with zero configs clearing the gates is
recorded with direction "none" rather than simply omitted, so a quiet night
for an asset is a stated fact, not a gap you have to notice on your own.

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
- The meta-model is in fallback mode. It requires 30 distinct settle dates
  with a labeled outcome before it trains on real data, not 30 labeled
  rows — several configs stamped the same night are correlated (same
  market data, same regime) and carry roughly one night's worth of
  independent evidence between them, not one row's worth each. It does not
  have 30 yet. Until then its ranking is a heuristic, not a learned model,
  and the repo says so.
- Roughly half of all asset-nights produce no config that clears Monte
  Carlo gating. That is the gating doing its job, not a malfunction — but
  it's also why the record accumulates slowly.
- Long-only. No shorting, in any market condition.
- One laptop, one person. No redundancy. Missed nights have happened and are
  published as PIPELINE_FAILURE entries.

## Current state

As of 2026-07-29: 13 chain entries, 4 predictions (1 gradeable, the rest
"none"/no-signal), 0 labeled outcomes (0 of 30 distinct settle dates
required for meta-model graduation). Multi-config stamping — every
MC-passed config, not just the top pick — begins with the 2026-07-30 cycle;
the change itself is documented in a METHODOLOGY_CHANGE chain entry.

Do not trust this paragraph — it goes stale. Run verify_chain.py for the
live count.

## Layout

    nightshift/cycle.py             the 7-stage nightly pipeline
    nightshift/stamp_prediction.py  writes the gradeable claim
    nightshift/label_outcomes.py    grades it 7 days later
    nightshift/publish_chain.py     appends to the hash chain
    nightshift/verify_chain.py      recomputes it from genesis
    nightshift/anchor_ots.py        timestamps entry hashes to Bitcoin
    reports/chain.jsonl             the record
    reports/predictions.jsonl       open and settled predictions
    reports/ots/                    OpenTimestamps proofs, one per anchored entry (since 2026-07-28)

## Run it

    python3 run_nightshift.py             # full cycle, live Binance data
    python3 nightshift/label_outcomes.py  # grade anything that has settled
    python3 nightshift/verify_chain.py    # check the chain
