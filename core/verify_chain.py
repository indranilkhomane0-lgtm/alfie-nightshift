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
the same return numbers again restricted to the code version currently
frozen for the strategy (see FROZEN_CODE_VERSION below) -- that last one
is the only record that will ever be sellable, since everything before it
mixes 108 different code versions into one win/loss tally.
"""

import hashlib
import json
import statistics
import sys
from pathlib import Path

CHAIN_PATH = Path(__file__).resolve().parent.parent / "reports" / "chain.jsonl"
GENESIS_HASH = "0" * 64

# Strategy code freeze declared 2026-09-14, chain entry 234
# (METHODOLOGY_CHANGE): code frozen at this exact code_version (git HEAD
# sha at chain time), first seen at entry 223. Freeze lifts at 30 distinct
# labeled dates chained under it -- see that entry for the full terms.
# Literal, not imported from nightshift.config: this file intentionally
# has zero repo imports so it keeps running with nothing but the stdlib.
FROZEN_CODE_VERSION = "0632a1ddb5e989b1725d2f29bb80b02eb3903b8b"

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


def compute_stats(chain_path: Path) -> dict:
    """Walk the chain from genesis, verifying every hash link, and return
    every number main() prints. Raises ChainBroken at the first mismatch."""
    prev = GENESIS_HASH
    wins = losses = waits = failures = 0
    code_versions = {}  # code_version -> 1-based index of first appearance
    # (asset, direction, cycle_date) -> {"outcome", "return_pct",
    # "code_version"}. Multiple configs can agree on the same
    # asset/direction/night -- same entry price, same settle price, same
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
                }
                settle_date = p.get("settle_date")
                if settle_date:
                    labeled_dates.add(settle_date)

    distinct_wins = sum(1 for c in distinct_calls.values() if c["outcome"] == "WIN")
    distinct_losses = sum(1 for c in distinct_calls.values() if c["outcome"] == "LOSS")

    def returns(outcome, frozen=False):
        return [
            c["return_pct"] for c in distinct_calls.values()
            if c["outcome"] == outcome and c["return_pct"] is not None
            and (not frozen or c["code_version"] == FROZEN_CODE_VERSION)
        ]

    win_returns, loss_returns = returns("WIN"), returns("LOSS")
    frozen_win_returns, frozen_loss_returns = returns("WIN", frozen=True), returns("LOSS", frozen=True)

    return {
        "entry_count": i,
        "wins": wins, "losses": losses, "waits": waits, "failures": failures,
        "distinct_wins": distinct_wins, "distinct_losses": distinct_losses,
        "outcome_conflicts": outcome_conflicts,
        "code_versions": code_versions,
        "labeled_dates_count": len(labeled_dates),
        "return_stats": {
            "win": (*_mean_median(win_returns), len(win_returns)),
            "loss": (*_mean_median(loss_returns), len(loss_returns)),
        },
        "frozen_return_stats": {
            "win": (*_mean_median(frozen_win_returns), len(frozen_win_returns)),
            "loss": (*_mean_median(frozen_loss_returns), len(frozen_loss_returns)),
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

    rs = stats["return_stats"]
    print("return per graded call (all code versions):")
    print(f"  wins:   {_print_return_stats(rs['win'])}")
    print(f"  losses: {_print_return_stats(rs['loss'])}")

    print(
        f"labeled dates: {stats['labeled_dates_count']} of {LABELED_DATES_TARGET} "
        f"distinct labeled dates"
    )

    frs = stats["frozen_return_stats"]
    print(f"return per graded call (code_version {FROZEN_CODE_VERSION[:12]} only -- post-freeze record):")
    print(f"  wins:   {_print_return_stats(frs['win'])}")
    print(f"  losses: {_print_return_stats(frs['loss'])}")

    print(f"code versions on record: {len(stats['code_versions'])}")
    for cv, idx in sorted(stats["code_versions"].items(), key=lambda kv: kv[1]):
        is_sha = cv not in ("unknown", "(predates code_version)")
        label = cv[:12] if is_sha else cv
        print(f"  {label:23s} first appears at entry {idx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
