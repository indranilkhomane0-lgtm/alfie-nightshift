# Alfie Night Shift

An autonomous crypto signal pipeline that publishes its own track record —
wins, losses, no-calls, and failed nights — to a tamper-evident hash chain.

**The product is not the signals. The product is the record.**
You do not have to take anything here on faith. You can verify it yourself,
with one command and no dependencies.

## Verify the record yourself

    git clone https://github.com/indranilkhomane0-lgtm/alfie-nightshift
    cd alfie-nightshift
    python3 core/verify_chain.py

(`python3 nightshift/verify_chain.py` still works -- it forwards to the
path above.)

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
  (regime, ranked configs, live-health section), referenced by its own
  sha256 rather than embedded in full. The live-health section always
  reads "No strategies currently deployed" — see the disclosure below.
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

Every strategy in this repo is long-only, so the stamper never emits "short".
It emits "long" when the strategy's entry condition is true today, and "none"
when there is no entry signal. A third value, "neutral", also appears in the
record — see reports/predictions.jsonl for 2026-07-24 — and is graded
NO_CALL exactly like "none". Earlier versions of this section said only
"long" and "none" were possible; that was wrong, and the record disagreed
with it. The rule that matters is unchanged: anything other than "long" is
NO_CALL and is excluded from the corpus. The record must not claim a call the
system never made.

## Outcomes are graded by a fixed rule

nightshift/label_outcomes.py runs daily. When a settle date arrives, it
fetches that day's daily close from Binance — the same source as the entry
price — and grades against a rule declared in advance and never changed
retroactively:

    long -> WIN if settle_close > entry_price, else LOSS
    none -> NO_CALL (excluded from the corpus entirely)

The labeled outcome is then chained. NO_CALL exists so the record never
takes credit for a call it did not make.

Two properties of that rule, stated rather than left to be discovered:

**A flat close is a LOSS.** The comparison is strictly greater-than, so
`settle_close == entry_price` grades LOSS, not WIN and not a draw. There is
no draw category. At these price scales exact equality is vanishingly
unlikely, and grading a flat outcome as a loss is the conservative
direction — but it is an asymmetry, it is in the code, and it should not be
something a reader finds by reading `grade()` themselves.

**Grading happens the day after the settle date, not on it.** A daily close
does not exist until its UTC day has ended; Binance returns an in-progress
candle whose "close" is merely the last trade so far. `fetch_settle_close()`
returns None until the settle day's bar is final, and the due-check requires
the settle date to be strictly before today. So a prediction settling on the
3rd is graded on the 4th. An outcome that has not appeared on its settle
date is the rule working, not the labeler failing.

## The nightly pipeline

Seven stages, run against BTC/USDT, ETH/USDT, and SOL/USDT on Binance:

1. Ingest — 500 days of OHLCV plus derivatives signals
2. Collect pending outcomes — intended to pull results from
   live-monitored configs. In the current code this is a no-op every
   night: see "Known defect — live monitoring never runs" below.
3. Regime classification — 4-state HMM (low-vol bull, high-vol bull,
   choppy/ranging, crisis/bear); each regime gates which strategy families
   are eligible to run at all
4. Walk-forward optimisation — intended to be an out-of-sample parameter
   search per family per asset, with a minimum OOS Sharpe floor. The
   parameter-selection step is not actually held out from the data it's
   later scored against: see "Known defect — WFO metrics are not
   genuinely out-of-sample" below.
5. Monte Carlo gating — 5 gates covering noise, slippage, parameter
   perturbation, and block resampling; a config must pass all five
6. Meta-model ranking — intended to rank survivors with a trained
   survival model. It has never trained on real data (see below) and
   currently always falls back to ranking by GT-Score instead.
7. Live monitor + brief — intended to run decay checks and auto-suspend
   before the brief is written and chained. Neither has ever executed;
   the brief is still written and chained every night regardless.

### Known defect — live monitoring never runs

`LiveMonitor.register()` (nightshift/live_monitor.py) is the method that
adds a deployed config to the monitor's tracking dict. It has never been
called from anywhere in the pipeline, in any commit since the system's
first commit, 607da1f ("Add Alfie Night Shift — 7-stage autonomous
overnight cycle", 2026-06-18). The monitor's internal dict is therefore
always empty, which means:

- Stage 2 (`collect_pending_outcomes`) never has any live performance to
  read, so it never writes a `survived` or `decay_ratio` label onto a
  corpus row. As of this writing, zero of the real rows in `corpus.db`
  have ever received either label.
- Stage 7's decay checks and drawdown-based early-stop
  (`LiveMonitor.check()`) have never run on a real config, no row has
  ever been written to the `live_monitor` table, and the auto-suspend
  branch in `cycle.py` has never fired.
- The meta-model's real-data training path requires 30 distinct labeled
  settle dates, which requires the labels above. It has therefore never
  trained on real data and cannot graduate out of fallback mode until
  this is fixed — not merely because the corpus is still small, but
  because nothing on the real path currently produces the label at all.
  (Training has occurred exactly once, on 60 fully synthetic `DEMO_*`
  rows seeded by `run_nightshift.py --demo`, which write the labels
  directly and bypass `LiveMonitor` entirely; that run is not part of
  the real corpus.)

This is disclosed on the chain as a METHODOLOGY_CHANGE entry published
2026-08-01. It is a disclosure, not yet a fix — `register()` is still
unwired as of this commit.

### Known defect — WFO metrics are not genuinely out-of-sample

`WFOEngine.optimise()` (nightshift/wfo_engine.py) sets `train_all = prices`
(line 89) — the full ~500-day price series for the asset, not a slice held
out from any fold. The Optuna objective (lines 92-98) scores every
candidate parameter set by running it against that full series, and
`top_params` (lines 104-113) is selected purely by that full-history
score. Only afterward are the already-chosen parameter sets re-evaluated
on windowed fold slices drawn from the same series (lines 119-133), and
those returns are the ones labeled OOS and used for `sharpe_oos`,
`gt_score_oos`, `max_dd_oos`, `sortino_oos`, `calmar_oos`, and
`win_rate_oos` (lines 136-151).

Because parameter selection already used performance on the very days
later scored as "out-of-sample," those days were never actually held
out, and `MIN_OOS_SHARPE` (the gate at line 141) has been filtering on
an inflated metric rather than a genuine holdout Sharpe. Every
`sharpe_oos`, `gt_score_oos`, and `max_dd_oos` figure printed in a
brief or written to a corpus row, for every night this system has run,
should be read as in-sample-influenced, not as a true holdout result.

This does **not** affect the integrity of the record itself. Predictions
are still stamped before settlement with a fixed entry price and settle
date (nightshift/stamp_prediction.py), and graded afterward against real
Binance closes by a rule fixed in advance and never changed retroactively
(nightshift/label_outcomes.py). The WIN/LOSS/NO_CALL grading on the chain
does not depend on `wfo_engine.py` and is unaffected. This is a defect in
how candidate configs are scored and selected, not a tampering or
grading-integrity issue.

The defect has existed since the system's initial commit, 607da1f
(2026-06-18), and is present in every WFO run to date. This is disclosed
on the chain as a METHODOLOGY_CHANGE entry published 2026-08-01. It is a
disclosure, not yet a fix — the objective in `wfo_engine.py` is still
unchanged as of this commit.

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
  independent evidence between them, not one row's worth each. As of
  2026-08-02 it has zero, and ranking is a heuristic (GT-Score), not a
  learned model.

  **Graduating it would change very little, and this repo previously
  implied otherwise.** Since multi-config stamping began on 2026-07-30,
  *every* config that clears Monte Carlo gating is stamped and graded —
  not just the top-ranked one. The meta-model's ranking therefore decides
  which three configs are displayed in the nightly brief, and nothing
  else. It does not decide what is stamped, what is graded, what is
  chained, or what is published. Earlier versions of this file described
  meta-model graduation as the system's main blocker; that was true when
  one config per night was stamped and ranking selected it, and it stopped
  being true when multi-config stamping landed. The roadmap was not
  updated at the time. It is now.

  How long graduation would take, measured rather than estimated: over
  2026-07-24 to 2026-08-02, 55 configs cleared all five MC gates and 12
  had a live entry signal (21.8%). Three of eight nights produced at least
  one gradeable (`long`) prediction. Since graduation counts distinct
  settle dates, stamping more configs per night cannot accelerate it —
  only the share of *nights* with at least one firing signal matters. At
  the observed rate that is roughly 79 productive nights, about three
  months, and two of those three gradeable nights were later voided.

- The previous entry in this list claimed the meta-model "cannot leave
  fallback as the code currently stands," because `LiveMonitor.register()`
  was never called. That specific blocker no longer applies: as of
  2026-08-02 the meta-model trains on WIN/LOSS outcomes from
  `reports/predictions.jsonl` rather than on a decay ratio derived from
  live monitoring, so `LiveMonitor` is not on the path at all. See the
  2026-08-02 METHODOLOGY_CHANGE chain entry. `LiveMonitor` remains
  unwired and is now vestigial rather than blocking.
- The WFO "out-of-sample" Sharpe/GT-Score/max-drawdown figures are not
  currently genuine holdout results — parameter selection uses the same
  data later scored as OOS, so `MIN_OOS_SHARPE` gating and every reported
  WFO metric to date are in-sample-influenced. This does not affect
  outcome grading (predictions are still stamped before settlement and
  graded against real closes by a rule fixed in advance) — it affects
  which configs get selected and how good they're reported to look. See
  "Known defect — WFO metrics are not genuinely out-of-sample" above and
  the 2026-08-01 METHODOLOGY_CHANGE chain entry.
- Roughly half of all asset-nights produce no config that clears Monte
  Carlo gating. That is the gating doing its job, not a malfunction — but
  it's also why the record accumulates slowly.
- Separately, and more limiting: of the configs that *do* clear gating,
  only 21.8% had a live entry signal on the night they were stamped (12 of
  55, 2026-07-24 to 2026-08-02). A config can pass every gate and still
  emit `none` because its entry condition — an RSI band, a z-score
  threshold — simply is not true that day. That is correct behaviour, not
  a defect, but it means the gates are not what limits how fast the record
  accumulates gradeable outcomes. Loosening them would add configs that
  mostly still would not fire.
- Long-only. No shorting, in any market condition.
- One laptop, one person. No redundancy. Missed nights have happened and are
  published as PIPELINE_FAILURE entries.

## Current state

The chain holds NIGHTLY_BRIEF, LABELED_OUTCOME, PIPELINE_FAILURE,
METHODOLOGY_CHANGE, and VOID_DECISION entries. Multi-config stamping —
every MC-passed config, not just the top pick — begins with the
2026-07-30 cycle; the change itself is documented in a METHODOLOGY_CHANGE
chain entry. The meta-model needs 30 distinct settle dates with a labeled
outcome before it trains on real data and has none yet — and, per the
2026-08-01 METHODOLOGY_CHANGE entry, cannot acquire any under the current
code, since the live-monitoring step that would label them is never
invoked (see "Known defect — live monitoring never runs" above).

Entry counts, open predictions, and labeled outcomes change every night —
run `python3 core/verify_chain.py` for the live numbers rather than
trusting a count written here.

## Layout

    nightshift/cycle.py             the 7-stage nightly pipeline
    nightshift/stamp_prediction.py  writes the gradeable claim
    nightshift/label_outcomes.py    grades it 7 days later
    nightshift/publish_chain.py     appends to the hash chain
    core/verify_chain.py            recomputes it from genesis (nightshift/verify_chain.py is a compat shim that forwards here)
    nightshift/anchor_ots.py        timestamps entry hashes to Bitcoin
    reports/chain.jsonl             the record
    reports/predictions.jsonl       open and settled predictions
    reports/ots/                    OpenTimestamps proofs, one per anchored entry (since 2026-07-28)

## Run it

    python3 run_nightshift.py             # full cycle, live Binance data
    python3 nightshift/label_outcomes.py  # grade anything that has settled
    python3 core/verify_chain.py          # check the chain
