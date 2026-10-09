#!/usr/bin/env python3
"""
Alfie Night Shift — chain verifier.

Anyone can run this against reports/chain.jsonl with zero dependencies:

    python3 verify_chain.py

It recomputes every hash from genesis. If any historical entry was edited,
deleted, or reordered, verification fails at that exact line.
This script is the product's honesty claim, made executable.

Also reports the numbers a win/loss count alone hides: average and median
return per graded call (separately for wins and losses -- a 47% win rate
means nothing without knowing what winning and losing are each worth),
distinct labeled dates against the meta-model's graduation threshold, and
the same return numbers again restricted to the current frozen strategy
version -- that slice is the only one not mixing multiple code versions
into one win/loss tally; everything else on this record combines over a
hundred different versions into a single count.

Six freeze generations are tracked, not one -- see FROZEN_CODE_VERSION,
FROZEN_STRATEGY_VERSION_V1, FROZEN_STRATEGY_VERSION_V2,
FROZEN_STRATEGY_VERSION_V3, FROZEN_STRATEGY_VERSION_V4, and
FROZEN_STRATEGY_VERSION below. The first
freeze (chain entry 234) was declared against code_version, the git HEAD
sha at chain time. That conflates strategy changes with every
tooling/infra commit made under the freeze, silently: an anchoring fix or
a logging tweak moves code_version exactly as much as a change to signal
logic would, so code_version cannot actually tell the two apart. The
first correction (the entry immediately after 234) repointed the freeze
to strategy_version -- a content hash over only the files that can change
what signal gets emitted (see nightshift/strategy_version.py), computed
independently of git history. That hash is now FROZEN_STRATEGY_VERSION_V1:
its surface excluded nightshift/meta_model.py, traced and confirmed
non-causal (it gates nothing today -- see that module's docstring). The
second correction added meta_model.py back as a conservative buffer
against a future wiring change moving the system without moving the
hash, which produced FROZEN_STRATEGY_VERSION_V2. The third correction
(2026-09-23) fixed a frozen-surface file (cycle.py) directly rather than
repointing which files are hashed: the nightly brief's meta-model
progress line called corpus_size() -- a permanently-zero,
LiveMonitor-dependent counter disclosed as dead in the 2026-08-01
METHODOLOGY_CHANGE -- where it should have called
labeled_prediction_date_count(), the counter the meta-model's own
training gate actually uses. A permitted display/logging fix (entry 234's
permitted-during-freeze terms), but cycle.py is hashed whole with no
carve-out for non-causal spans (see strategy_version.py: "glue code is as
signal-determining as the logic it calls"), so fixing it moved the hash
regardless of the change being cosmetic. That produced
FROZEN_STRATEGY_VERSION_V3. The fourth correction added
nightshift/stamp_prediction.py's raw-OHLCV archiving call (archiving the
exact closed-bar window _context_hash() hashes, into
reports/archive/ohlcv.csv.gz, so a third party can verify a call instead
of trusting context_hash alone) -- again a change to a frozen-surface
file with zero effect on what signal gets computed or which call gets
published, and again moving the hash regardless, for the same
no-carve-out reason as the third correction. That produced
FROZEN_STRATEGY_VERSION_V4. The fifth correction (2026-10-09) fixed
should_retrain()'s graduation gate in nightshift/meta_model.py: its
pre-training branch checked labeled_date_count()/corpus_size(), both
dependent on corpus.survived -- written only via a call chain ending at
LiveMonitor.register(), which has zero callers anywhere in this
codebase, making both counters permanently zero against the real
corpus. The gate sat above two layers that were already correct:
train()'s own internal n_dates gate and _df()'s training rows both
already used labeled_prediction_date_count()/get_prediction_labeled_
corpus(). should_retrain() is now repointed to that same source, both
branches (the post-training 20% growth trigger had the identical
corpus_size() dependency and was fixed in the same pass -- fixing only
the pre-training branch would have graduated the model once and then
never retrained on growth again). This is a gating fix, not a change
to how a call is made: nightshift/cycle.py also moved, for the
graduation-sidecar write that now feeds a dedicated chain entry on the
fallback-to-trained transition. Both files are on the frozen surface
and are hashed whole with no carve-out, so both moving the hash
regardless of causal relevance is the same no-carve-out reason as the
third and fourth corrections. That produced the current value,
FROZEN_STRATEGY_VERSION. All six constants and all six filtered
sections stay: each slice is real history that already happened under
those terms and does not get erased by the next correction. Note what
does NOT reset here, since it's easy to conflate with what does:
LABELED_DATES_TARGET progress (below) counts distinct settle dates across
the whole predictions record, unfiltered by strategy_version -- changing
the frozen surface starts a new performance slice, it does not zero the
graduation count.
"""

import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

CHAIN_PATH = Path(__file__).resolve().parent.parent / "reports" / "chain.jsonl"
OTS_DIR = Path(__file__).resolve().parent.parent / "reports" / "ots"
GENESIS_HASH = "0" * 64

# Entries published before this date predate anchoring and are covered by
# the hash chain alone -- no receipt is expected, and none is backfilled.
# Keep in sync with nightshift/anchor_ots.py's ANCHORING_START and
# nightshift/self_audit.py's OTS_ANCHORING_START.
ANCHORING_START = "2026-07-28"

# First 32 bytes of every well-formed OpenTimestamps proof file (the
# literal ASCII "\x00OpenTimestamps\x00\x00Proof\x00" header plus its
# fixed magic suffix) -- confirmed against a real .hash.ots in this
# repo's reports/ots/, not taken on faith from the spec. A structural
# check only: this confirms the file at least looks like a real OTS
# proof, not that Bitcoin has attested to it. That deeper check needs
# the `ots` CLI plus either a local Bitcoin node or a block explorer to
# query -- exactly the dependency this script exists to avoid (see
# README's Bitcoin anchoring section, "the honest limit"). Run
# `ots verify <file>` yourself for that.
OTS_MAGIC = bytes.fromhex(
    "004f70656e54696d657374616d7073000050726f6f6600bf89e2e884e89294"
)

# Attestation tag bytes from python-opentimestamps's own TimeAttestation
# subclasses (opentimestamps/core/notary.py: PendingAttestation.TAG,
# BitcoinBlockHeaderAttestation.TAG) -- a proof file contains one of
# these as the leaf of each calendar's branch in its operation tree.
# PENDING_TAG is written at stamp time and is NEVER removed by a later
# `ots upgrade` -- an upgrade adds a CONFIRMED_TAG leaf alongside it, it
# doesn't replace it. So PENDING_TAG's presence says nothing about
# whether the proof is still pending; only CONFIRMED_TAG's presence (at
# least one calendar's branch resolved to an actual Bitcoin block) means
# that. Cross-checked against `ots info` on a random 40-file sample of
# this repo's real reports/ots/ -- 0 mismatches between this raw-byte
# search and the CLI's own classification. Structural only, like
# OTS_MAGIC above: confirms the proof file EMBEDS a confirmed-attestation
# structure, not that that structure's Bitcoin block header and merkle
# path actually check out -- `ots verify <file>` against a real node (or
# nightshift/anchor_ots.py's own opportunistic_upgrade(), which calls
# `ots upgrade`, the same deeper check) is still the honest ceiling. The
# anchor_ots.py bug these same two tags were pulled from fixing is
# disclosed on the chain: self_audit.py's independent ots_proof_coverage
# check (different code, same underlying tags) had failed every day
# since 2026-08-01 while this file's single "receipted" number stayed
# silent about it.
PENDING_ATTESTATION_TAG = bytes.fromhex("83dfe30d2ef90c8e")
CONFIRMED_ATTESTATION_TAG = bytes.fromhex("0588960d73d71901")

# Mirrors nightshift/self_audit.py's OTS_GRACE_HOURS -- Bitcoin
# confirmation is hours, not days, so a proof still pending past this
# age is a real anchoring gap, not normal lag. Kept as the same value,
# literally, for the same zero-repo-imports reason as every other
# constant mirrored from elsewhere in this file.
PENDING_GRACE_HOURS = 24

# Original freeze, declared 2026-09-14 (chain entry 234, METHODOLOGY_CHANGE):
# code frozen at this exact code_version (git HEAD sha at chain time), first
# seen at entry 223. Repointed to FROZEN_STRATEGY_VERSION_V1 below (see
# that constant for why) -- kept, filter and all, because the calls
# chained under this version between entries 224 and the repoint are real
# history, not an error to be erased. Literal, not imported from
# nightshift.config: this file intentionally has zero repo imports so it
# keeps running with nothing but the stdlib.
FROZEN_CODE_VERSION = "0632a1ddb5e989b1725d2f29bb80b02eb3903b8b"

# First repoint, declared 2026-09-16 (the METHODOLOGY_CHANGE entry
# immediately following 234): strategy frozen at this exact
# strategy_version -- a sha256 over the sorted contents of the 7 files
# that then made up nightshift/strategy_version.py:STRATEGY_VERSION_FILES
# (excluding meta_model.py -- see FROZEN_STRATEGY_VERSION below), not the
# git commit. Superseded by FROZEN_STRATEGY_VERSION -- kept, filter and
# all, for the same real-history reason FROZEN_CODE_VERSION was kept
# rather than dropped when this one superseded it.
FROZEN_STRATEGY_VERSION_V1 = "a7bdda9ce2654c0d24810b86c27c6e427b9f9aac7258ce98b5b8289acf3e79fb"

# Second repoint, declared 2026-09-16 (the METHODOLOGY_CHANGE entry after
# that): meta_model.py added to the 7-file surface as a conservative
# buffer -- it gates nothing today (LiveMonitor.register() is still never
# called from cycle.py), but a future wiring change that made it start
# gating deployment would otherwise move the system without moving the
# hash. Governed the freeze from that correction until the third below.
# Superseded -- kept, filter and all, for the same real-history reason
# the constants above were kept rather than dropped.
FROZEN_STRATEGY_VERSION_V2 = "e66ce4f8754a4445b21d5a1c1c0b166afc36e856b588814c719bfa4918e835e8"

# Third correction, declared 2026-09-23: not a repoint of which files are
# hashed (the 8-file surface is unchanged), but a permitted fix made
# directly to one of those files. cycle.py's brief-rendering call passed
# corpus_size() -- disclosed 2026-08-01 as permanently zero, dependent on
# LiveMonitor.register(), which is never called -- where it should have
# passed labeled_prediction_date_count(), the counter
# nightshift/meta_model.py's own training gate actually uses. A checked
# alternative -- moving the brief-rendering call off the frozen surface
# entirely, the way failure classification was routed through
# run_nightshift.py rather than argued as exempt -- was not available
# here: the brief is written to disk from inside cycle.py's own Stage 7,
# interleaved with prediction-stamping that depends on the same local
# state, and strategy_version.py hashes cycle.py whole with no carve-out
# for non-causal spans ("glue code is as signal-determining as the logic
# it calls"). The fix was made in place and disclosed. Governed the
# freeze from that correction until the fourth below. Superseded --
# kept, filter and all, for the same real-history reason the constants
# above were kept rather than dropped.
FROZEN_STRATEGY_VERSION_V3 = "06d869d35c9922053016cd0b316ea5ceffc6b58906239dbbe2e12e65359887e5"

# Fourth correction, declared 2026-10-06/07: approved explicitly as an
# archiving-only freeze reset ("breaking a freeze while the slice is
# n=18 costs less than breaking one that has become the headline
# number" -- the meta-model graduates at n=30 labeled dates and this
# landed ahead of that on purpose). nightshift/stamp_prediction.py
# gained one import and one call (archive_closed_bars(), see
# nightshift/archive_ohlcv.py) at the exact point `closed` -- the window
# _context_hash() is about to hash -- is already in hand, so the
# archived bytes are provably the same ones a call's context_hash
# commits to, not an independent re-fetch. No change to entry_signal(),
# direction, entry_price, or which config gets ranked/selected -- this
# moves the hash for the same no-carve-out reason the third correction
# did (cycle.py/stamp_prediction.py are hashed whole), not because
# anything about how a call is made, changed. Governed the freeze from
# this correction until the fifth below. Superseded -- kept, filter and
# all, for the same real-history reason the constants above were kept
# rather than dropped.
FROZEN_STRATEGY_VERSION_V4 = "2dd2221ce744f26e5dad3367a080a118329f22c9020860a0e950c2797985fee0"

# Fifth correction, declared 2026-10-09: should_retrain()'s graduation
# gate (nightshift/meta_model.py) repointed from the permanently-zero,
# LiveMonitor-dependent labeled_date_count()/corpus_size() pair to
# labeled_prediction_date_count()/get_prediction_labeled_corpus() -- the
# same source train()'s own internal gate and _df()'s training rows
# already used, so the broken gate sat above two layers that were
# already correct. Both the pre-training branch and the post-training
# 20% growth trigger were fixed in the same pass (same dead dependency,
# via corpus_size()); fixing only the first would have graduated the
# model once and then never retrained on growth again.
# nightshift/cycle.py also moved, for the graduation-sidecar write that
# now feeds a dedicated METHODOLOGY_CHANGE chain entry on the
# fallback-to-trained transition. This is a gating fix, not a change to
# how a call is made -- no change to entry_signal(), direction,
# entry_price, or which config gets ranked/selected -- but moves the
# hash for the same no-carve-out reason the third and fourth
# corrections did (meta_model.py/cycle.py are hashed whole). This is
# the value that actually governs the freeze from this correction
# forward. Literal here for the same zero-repo-imports reason as the
# constants above.
FROZEN_STRATEGY_VERSION = "0abecbf74ce81a865c95aa45e3859582558292110d451c648ceabb50007fed8e"

# Meta-model graduation gate (nightshift/config.py META_MIN_SAMPLES) --
# distinct labeled dates, not distinct calls or rows: same-night calls
# share market data/regime and carry roughly one date's worth of
# independent evidence, not one call's worth each. Kept as a literal
# here for the same zero-repo-imports reason as FROZEN_CODE_VERSION.
LABELED_DATES_TARGET = 30


class ChainBroken(Exception):
    """Raised at the first hash-link failure, carrying the printable
    reason. compute_stats() must never return partial stats for a chain
    that failed verification -- a caller printing return/win-rate numbers
    computed from an edited chain would be worse than printing nothing."""


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _mean_median(values):
    """(mean, median) for a list of floats, or (None, None) if empty --
    None must print as "n/a", never as 0.0, which would misreport an
    absence of data as a zero return."""
    if not values:
        return None, None
    return statistics.mean(values), statistics.median(values)


def _receipt_status(entry_hash: str, ots_dir: Path) -> tuple[str, float | None]:
    """("missing" | "invalid" | "pending" | "confirmed", proof_age_hours)
    for one entry's OpenTimestamps proof. proof_age_hours is None unless
    status is "pending" -- it's the .hash.ots file's own mtime, not the
    chain entry's published_at_utc, because a backfilled proof (stamped
    well after its entry's original night, e.g. by a later backlog
    sweep) is stamped today and cannot possibly be confirmed yet; ageing
    it from the entry's publish date would flag it as stale the instant
    it's written, which says nothing about whether anything is wrong.
    Same convention as nightshift/self_audit.py's pending_past_grace.

    Structural only -- see OTS_MAGIC and the two attestation tags above
    for exactly what "confirmed" does and doesn't prove. Three ways to
    fail, all surfaced as "invalid" rather than raising: the .hash
    sidecar exists but its content doesn't match entry_hash (corrupted
    or mismatched sidecar); the .hash.ots file is too short or doesn't
    start with OTS_MAGIC (truncated, corrupted, or not actually an OTS
    proof); or the file can't be read at all (permissions, race with a
    concurrent write)."""
    ots_file = ots_dir / f"{entry_hash}.hash.ots"
    if not ots_file.exists():
        return "missing", None
    try:
        hash_file = ots_dir / f"{entry_hash}.hash"
        if hash_file.exists() and hash_file.read_text().strip() != entry_hash:
            return "invalid", None
        raw = ots_file.read_bytes()
        if raw[:len(OTS_MAGIC)] != OTS_MAGIC:
            return "invalid", None
        if CONFIRMED_ATTESTATION_TAG in raw:
            return "confirmed", None
        # A well-formed proof always embeds at least a pending calendar
        # commitment (that's what `ots stamp` writes at publish time) --
        # if neither tag is found, something about this file's structure
        # is off in a way OTS_MAGIC alone didn't catch. Honest about that
        # rather than silently calling it "pending".
        if PENDING_ATTESTATION_TAG not in raw:
            return "invalid", None
        age_hours = (time.time() - ots_file.stat().st_mtime) / 3600
        return "pending", age_hours
    except Exception:
        return "invalid", None


def compute_stats(chain_path: Path, ots_dir: Path = OTS_DIR) -> dict:
    """Walk the chain from genesis, verifying every hash link, and return
    every number main() prints. Raises ChainBroken at the first mismatch."""
    prev = GENESIS_HASH
    receipt_counts = {"confirmed": 0, "pending": 0, "missing": 0, "invalid": 0}
    missing_entries = []   # 1-based line numbers, for "visible in the output"
    invalid_entries = []
    stale_pending_entries = []  # 1-based line numbers, pending past PENDING_GRACE_HOURS
    predates_anchoring = 0
    wins = losses = waits = failures = 0
    code_versions = {}  # code_version -> 1-based index of first appearance
    # (asset, direction, cycle_date) -> {"outcome", "return_pct",
    # "code_version", "strategy_version"}. Multiple configs can agree on
    # the same asset/direction/night -- same entry price, same settle price, same
    # graded outcome -- and each still gets its own chained LABELED_OUTCOME
    # entry. Collapsing on this key is what makes the headline numbers
    # (distinct calls made, and return per call) count calls, not configs
    # that agreed. Same key status.py uses, for the same reason.
    distinct_calls = {}
    outcome_conflicts = 0  # same key, different outcome across entries -- should never happen
    # Distinct settle dates carrying >=1 WIN/LOSS -- what META_MIN_SAMPLES
    # actually counts (nightshift/db.py labeled_prediction_date_count), not
    # the number of graded calls. Multiple calls can settle the same day.
    labeled_dates = set()

    i = 0
    with chain_path.open() as f:
        for i, line in enumerate(f, 1):
            entry = json.loads(line)
            if entry["prev_hash"] != prev:
                raise ChainBroken(f"BROKEN at line {i}: prev_hash mismatch")
            expect = hashlib.sha256(
                prev.encode() + canonical(entry["payload"])
            ).hexdigest()
            if entry["entry_hash"] != expect:
                raise ChainBroken(f"BROKEN at line {i}: entry_hash mismatch (edited payload)")
            prev = entry["entry_hash"]

            # Every entry gets checked, not just LABELED_OUTCOME -- anchor_ots.py
            # stamps every chained hash regardless of payload type. published_at_utc
            # (top-level, not payload) is the same field anchor_ots.py's own
            # unanchored_entries() filters on.
            if entry.get("published_at_utc", "")[:10] < ANCHORING_START:
                predates_anchoring += 1
            else:
                status, age_hours = _receipt_status(entry["entry_hash"], ots_dir)
                receipt_counts[status] += 1
                if status == "missing":
                    missing_entries.append(i)
                elif status == "invalid":
                    invalid_entries.append(i)
                elif status == "pending" and age_hours is not None and age_hours > PENDING_GRACE_HOURS:
                    stale_pending_entries.append(i)

            p = entry["payload"]
            cv = p.get("code_version", "(predates code_version)")
            if cv not in code_versions:
                code_versions[cv] = i
            if p.get("type") == "PIPELINE_FAILURE":
                failures += 1
            outcome = str(p.get("outcome", "")).upper()
            if outcome == "WIN":
                wins += 1
            elif outcome == "LOSS":
                losses += 1
            elif p.get("signal") == "WAIT":
                waits += 1
            if outcome in ("WIN", "LOSS"):
                key = (p.get("asset"), p.get("direction"), p.get("cycle_date"))
                prior = distinct_calls.get(key)
                if prior is not None and prior["outcome"] != outcome:
                    outcome_conflicts += 1
                distinct_calls[key] = {
                    "outcome": outcome,
                    "return_pct": p.get("return_pct"),
                    "code_version": cv,
                    # .get(), not cv-style default: entries chained before
                    # the strategy_version repoint simply lack this key.
                    # None must stay None here -- absent, not inferred as
                    # "(predates strategy_version)" or coerced to a fake
                    # match/non-match sentinel. The filter below already
                    # does the right thing with None (never equals a real
                    # hash), so no special-casing is needed downstream.
                    "strategy_version": p.get("strategy_version"),
                }
                settle_date = p.get("settle_date")
                if settle_date:
                    labeled_dates.add(settle_date)

    distinct_wins = sum(1 for c in distinct_calls.values() if c["outcome"] == "WIN")
    distinct_losses = sum(1 for c in distinct_calls.values() if c["outcome"] == "LOSS")

    def returns(outcome, code_version=None, strategy_version=None):
        """return_pct list for distinct calls matching outcome, optionally
        further filtered to an exact code_version and/or strategy_version.
        A call whose stored value is None (field absent -- predates that
        field's introduction) can never match a real filter value, so it's
        excluded rather than counted as a false match -- this is the
        "absent, not zero, not inferred" rule applied to filtering, not
        just to display."""
        return [
            c["return_pct"] for c in distinct_calls.values()
            if c["outcome"] == outcome and c["return_pct"] is not None
            and (code_version is None or c["code_version"] == code_version)
            and (strategy_version is None or c["strategy_version"] == strategy_version)
        ]

    win_returns, loss_returns = returns("WIN"), returns("LOSS")
    frozen_cv_win = returns("WIN", code_version=FROZEN_CODE_VERSION)
    frozen_cv_loss = returns("LOSS", code_version=FROZEN_CODE_VERSION)
    frozen_sv1_win = returns("WIN", strategy_version=FROZEN_STRATEGY_VERSION_V1)
    frozen_sv1_loss = returns("LOSS", strategy_version=FROZEN_STRATEGY_VERSION_V1)
    frozen_sv2_win = returns("WIN", strategy_version=FROZEN_STRATEGY_VERSION_V2)
    frozen_sv2_loss = returns("LOSS", strategy_version=FROZEN_STRATEGY_VERSION_V2)
    frozen_sv3_win = returns("WIN", strategy_version=FROZEN_STRATEGY_VERSION_V3)
    frozen_sv3_loss = returns("LOSS", strategy_version=FROZEN_STRATEGY_VERSION_V3)
    frozen_sv4_win = returns("WIN", strategy_version=FROZEN_STRATEGY_VERSION_V4)
    frozen_sv4_loss = returns("LOSS", strategy_version=FROZEN_STRATEGY_VERSION_V4)
    frozen_sv_win = returns("WIN", strategy_version=FROZEN_STRATEGY_VERSION)
    frozen_sv_loss = returns("LOSS", strategy_version=FROZEN_STRATEGY_VERSION)

    return {
        "entry_count": i,
        "wins": wins, "losses": losses, "waits": waits, "failures": failures,
        "distinct_wins": distinct_wins, "distinct_losses": distinct_losses,
        "outcome_conflicts": outcome_conflicts,
        "code_versions": code_versions,
        "labeled_dates_count": len(labeled_dates),
        "receipt_counts": receipt_counts,
        "missing_receipt_entries": missing_entries,
        "invalid_receipt_entries": invalid_entries,
        "stale_pending_entries": stale_pending_entries,
        "predates_anchoring": predates_anchoring,
        "return_stats": {
            "win": (*_mean_median(win_returns), len(win_returns)),
            "loss": (*_mean_median(loss_returns), len(loss_returns)),
        },
        "frozen_code_version_return_stats": {
            "win": (*_mean_median(frozen_cv_win), len(frozen_cv_win)),
            "loss": (*_mean_median(frozen_cv_loss), len(frozen_cv_loss)),
        },
        "frozen_strategy_version_v1_return_stats": {
            "win": (*_mean_median(frozen_sv1_win), len(frozen_sv1_win)),
            "loss": (*_mean_median(frozen_sv1_loss), len(frozen_sv1_loss)),
        },
        "frozen_strategy_version_v2_return_stats": {
            "win": (*_mean_median(frozen_sv2_win), len(frozen_sv2_win)),
            "loss": (*_mean_median(frozen_sv2_loss), len(frozen_sv2_loss)),
        },
        "frozen_strategy_version_v3_return_stats": {
            "win": (*_mean_median(frozen_sv3_win), len(frozen_sv3_win)),
            "loss": (*_mean_median(frozen_sv3_loss), len(frozen_sv3_loss)),
        },
        "frozen_strategy_version_v4_return_stats": {
            "win": (*_mean_median(frozen_sv4_win), len(frozen_sv4_win)),
            "loss": (*_mean_median(frozen_sv4_loss), len(frozen_sv4_loss)),
        },
        "frozen_strategy_version_return_stats": {
            "win": (*_mean_median(frozen_sv_win), len(frozen_sv_win)),
            "loss": (*_mean_median(frozen_sv_loss), len(frozen_sv_loss)),
        },
    }


def _fmt_pct(x):
    return f"{x:+.2f}%" if x is not None else "n/a"


def _print_return_stats(stats):
    avg, median, n = stats
    return f"n={n}  avg {_fmt_pct(avg)}  median {_fmt_pct(median)}"


def main() -> int:
    if not CHAIN_PATH.exists():
        print("no chain file found at", CHAIN_PATH)
        return 1

    try:
        stats = compute_stats(CHAIN_PATH)
    except ChainBroken as exc:
        print(str(exc))
        return 1

    print(f"CHAIN VALID — {stats['entry_count']} entries, unbroken from genesis.")
    print(
        f"outcomes on record: {stats['distinct_wins']} wins / {stats['distinct_losses']} losses "
        f"({stats['distinct_wins'] + stats['distinct_losses']} distinct calls) / "
        f"{stats['waits']} waits / {stats['failures']} pipeline failures"
    )
    print(
        f"  {stats['wins']} win / {stats['losses']} loss chain entries (configs-in-agreement, "
        f"same asset+direction+night counted once above)"
    )
    if stats["outcome_conflicts"]:
        print(
            f"  CONFLICT: {stats['outcome_conflicts']} case(s) where configs agreeing on "
            f"asset+direction+night graded differently -- distinct-call counts "
            f"above are not reliable until this is investigated"
        )

    rc = stats["receipt_counts"]
    structurally_present = rc["confirmed"] + rc["pending"]
    print(
        f"OTS receipts (entries since {ANCHORING_START}): {structurally_present} structurally "
        f"present / {rc['missing']} missing / {rc['invalid']} invalid "
        f"({stats['predates_anchoring']} entries predate anchoring, no receipt expected, "
        f"not backfilled)"
    )
    print(
        f"  of those: {rc['confirmed']} confirmed (Bitcoin-attested) / "
        f"{rc['pending']} pending (calendar-committed only, not yet attested)"
    )
    if stats["stale_pending_entries"]:
        n = len(stats["stale_pending_entries"])
        print(
            f"  STALE: {n} of the {rc['pending']} pending receipt(s) are more than "
            f"{PENDING_GRACE_HOURS}h old and should have confirmed by now -- a real "
            f"anchoring gap, not normal Bitcoin-confirmation lag. If they still can't "
            f"upgrade, that's reported here, not quietly dropped: entry line(s) "
            f"{stats['stale_pending_entries']}"
        )
    if stats["missing_receipt_entries"]:
        print(f"  MISSING: entry line(s) {stats['missing_receipt_entries']}")
    if stats["invalid_receipt_entries"]:
        print(f"  INVALID: entry line(s) {stats['invalid_receipt_entries']}")
    if rc["missing"] or rc["invalid"] or stats["stale_pending_entries"]:
        print(
            "  a receipt gap here is not fatal to the chain -- the hash chain "
            "itself is unaffected -- but it is a real anchoring gap until "
            "nightshift/anchor_ots.py's next sweep clears it"
        )
    print(
        "  structural check only: confirms a well-formed OTS proof file exists "
        "and, for 'confirmed', that it embeds a Bitcoin-attestation structure -- "
        "not that attestation's block header and merkle path actually check out "
        "against the real blockchain. Run `ots verify <file>` against a Bitcoin "
        "node yourself for that deeper check"
    )

    rs = stats["return_stats"]
    print("return per graded call (all code versions):")
    print(f"  wins:   {_print_return_stats(rs['win'])}")
    print(f"  losses: {_print_return_stats(rs['loss'])}")

    print(
        f"labeled dates: {stats['labeled_dates_count']} of {LABELED_DATES_TARGET} "
        f"distinct labeled dates"
    )

    fcrs = stats["frozen_code_version_return_stats"]
    print(f"return per graded call (code_version {FROZEN_CODE_VERSION[:12]} only "
          f"-- original freeze, superseded twice; kept for history):")
    print(f"  wins:   {_print_return_stats(fcrs['win'])}")
    print(f"  losses: {_print_return_stats(fcrs['loss'])}")

    fs1rs = stats["frozen_strategy_version_v1_return_stats"]
    print(f"return per graded call (strategy_version {FROZEN_STRATEGY_VERSION_V1[:12]} only "
          f"-- first repoint (excluded meta_model.py), superseded; kept for history):")
    print(f"  wins:   {_print_return_stats(fs1rs['win'])}")
    print(f"  losses: {_print_return_stats(fs1rs['loss'])}")

    fs2rs = stats["frozen_strategy_version_v2_return_stats"]
    print(f"return per graded call (strategy_version {FROZEN_STRATEGY_VERSION_V2[:12]} only "
          f"-- second repoint (meta_model.py added as a conservative buffer), "
          f"superseded; kept for history):")
    print(f"  wins:   {_print_return_stats(fs2rs['win'])}")
    print(f"  losses: {_print_return_stats(fs2rs['loss'])}")

    fs3rs = stats["frozen_strategy_version_v3_return_stats"]
    print(f"return per graded call (strategy_version {FROZEN_STRATEGY_VERSION_V3[:12]} only "
          f"-- third correction (cycle.py brief-display fix, corpus_size() -> "
          f"labeled_prediction_date_count()), superseded; kept for history):")
    print(f"  wins:   {_print_return_stats(fs3rs['win'])}")
    print(f"  losses: {_print_return_stats(fs3rs['loss'])}")

    fs4rs = stats["frozen_strategy_version_v4_return_stats"]
    print(f"return per graded call (strategy_version {FROZEN_STRATEGY_VERSION_V4[:12]} only "
          f"-- fourth correction (raw-OHLCV archiving added to "
          f"stamp_prediction.py, archiving-only, no change to signal logic), "
          f"superseded; kept for history):")
    print(f"  wins:   {_print_return_stats(fs4rs['win'])}")
    print(f"  losses: {_print_return_stats(fs4rs['loss'])}")

    fsrs = stats["frozen_strategy_version_return_stats"]
    print(f"return per graded call (strategy_version {FROZEN_STRATEGY_VERSION[:12]} only "
          f"-- fifth correction (meta-model graduation gate repointed off "
          f"the permanently-zero labeled_date_count()/corpus_size() pair; "
          f"gating fix, no change to how a call is made); "
          f"current frozen strategy version):")
    print(f"  wins:   {_print_return_stats(fsrs['win'])}")
    print(f"  losses: {_print_return_stats(fsrs['loss'])}")

    print(f"code versions on record: {len(stats['code_versions'])}")
    for cv, idx in sorted(stats["code_versions"].items(), key=lambda kv: kv[1]):
        is_sha = cv not in ("unknown", "(predates code_version)")
        label = cv[:12] if is_sha else cv
        print(f"  {label:23s} first appears at entry {idx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
