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

Three freeze generations are tracked, not one -- see FROZEN_CODE_VERSION,
FROZEN_STRATEGY_VERSION_V1, and FROZEN_STRATEGY_VERSION below. The first
freeze (chain entry 234) was declared against code_version, the git HEAD
sha at chain time. That conflates strategy changes with every tooling/
infra commit made under the freeze, silently: an anchoring fix or a
logging tweak moves code_version exactly as much as a change to signal
logic would, so code_version cannot actually tell the two apart. The
first correction (the entry immediately after 234) repointed the freeze
to strategy_version -- a content hash over only the files that can change
what signal gets emitted (see nightshift/strategy_version.py), computed
independently of git history. That hash is now FROZEN_STRATEGY_VERSION_V1:
its surface excluded nightshift/meta_model.py, traced and confirmed
non-causal (it gates nothing today -- see that module's docstring). The
second correction added meta_model.py back as a conservative buffer
against a future wiring change moving the system without moving the hash,
which produced a new value -- FROZEN_STRATEGY_VERSION, the one that
governs from that correction forward. All three constants and all three
filtered sections stay: each slice is real history that already happened
under those terms and does not get erased by the next correction.
"""

import hashlib
import json
import statistics
import sys
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
# hash. This is the value that actually governs the freeze from this
# second correction forward. Literal here for the same zero-repo-imports
# reason as the constants above -- this file does not import nightshift.
# strategy_version to compute it live, it only compares the stored
# payload field against this pinned value.
FROZEN_STRATEGY_VERSION = "e66ce4f8754a4445b21d5a1c1c0b166afc36e856b588814c719bfa4918e835e8"

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


def _receipt_status(entry_hash: str, ots_dir: Path) -> str:
    """"receipted" | "missing" | "invalid" for one entry's OpenTimestamps
    proof. Structural only -- see OTS_MAGIC above for exactly what this
    does and doesn't confirm. Three ways to fail, all surfaced as
    "invalid" rather than raising: the .hash sidecar exists but its
    content doesn't match entry_hash (corrupted or mismatched sidecar);
    the .hash.ots file is too short or doesn't start with OTS_MAGIC
    (truncated, corrupted, or not actually an OTS proof); or the file
    can't be read at all (permissions, race with a concurrent write)."""
    ots_file = ots_dir / f"{entry_hash}.hash.ots"
    if not ots_file.exists():
        return "missing"
    try:
        hash_file = ots_dir / f"{entry_hash}.hash"
        if hash_file.exists() and hash_file.read_text().strip() != entry_hash:
            return "invalid"
        with ots_file.open("rb") as f:
            header = f.read(len(OTS_MAGIC))
        if header != OTS_MAGIC:
            return "invalid"
    except Exception:
        return "invalid"
    return "receipted"


def compute_stats(chain_path: Path, ots_dir: Path = OTS_DIR) -> dict:
    """Walk the chain from genesis, verifying every hash link, and return
    every number main() prints. Raises ChainBroken at the first mismatch."""
    prev = GENESIS_HASH
    receipt_counts = {"receipted": 0, "missing": 0, "invalid": 0}
    missing_entries = []   # 1-based line numbers, for "visible in the output"
    invalid_entries = []
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
                status = _receipt_status(entry["entry_hash"], ots_dir)
                receipt_counts[status] += 1
                if status == "missing":
                    missing_entries.append(i)
                elif status == "invalid":
                    invalid_entries.append(i)

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
    print(
        f"OTS receipts (entries since {ANCHORING_START}): {rc['receipted']} receipted / "
        f"{rc['missing']} missing / {rc['invalid']} invalid "
        f"({stats['predates_anchoring']} entries predate anchoring, no receipt expected, "
        f"not backfilled)"
    )
    if stats["missing_receipt_entries"]:
        print(f"  MISSING: entry line(s) {stats['missing_receipt_entries']}")
    if stats["invalid_receipt_entries"]:
        print(f"  INVALID: entry line(s) {stats['invalid_receipt_entries']}")
    if rc["missing"] or rc["invalid"]:
        print(
            "  a receipt gap here is not fatal to the chain -- the hash chain "
            "itself is unaffected -- but it is a real anchoring gap until "
            "nightshift/anchor_ots.py's next sweep clears it"
        )
    print(
        "  structural check only: confirms a well-formed OTS proof file "
        "exists, not that Bitcoin has attested to it -- run `ots verify "
        "<file>` yourself for that"
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

    fsrs = stats["frozen_strategy_version_return_stats"]
    print(f"return per graded call (strategy_version {FROZEN_STRATEGY_VERSION[:12]} only "
          f"-- second repoint (meta_model.py added as a conservative buffer); "
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
