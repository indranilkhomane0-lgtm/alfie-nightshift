"""
core/bar_calendar.py — bar-completeness primitives.

Market-agnostic home for "has this bar's data actually finalized yet"
logic, shared by any writer or grader that needs to avoid reading a
still-changing in-progress candle. core/ (not nightshift/) because this
is a market primitive, not a nightshift-pipeline concern -- and because
nightshift/stamp_prediction.py (a writer, commits context_hash before
settlement) and nightshift/label_outcomes.py (a grader, reads
settle_close after settlement) both need the SAME answer to "is this bar
closed," and a writer importing from a grader (or vice versa) is the
wrong dependency direction for two modules that are peers, not one
built on the other.
"""

from datetime import datetime, timezone


def is_utc_daily_bar_complete(bar_date) -> bool:
    """True once bar_date's UTC calendar day has fully elapsed, so a
    DAILY bar dated bar_date is final rather than the in-progress
    current-day candle whose "close" is just the last trade so far.
    bar_date may be a "YYYY-MM-DD" string, a date, or a datetime
    (including pd.Timestamp, a datetime subclass).

    ASSUMES CONTINUOUS, UTC-MIDNIGHT-ALIGNED DAILY BARS -- correct for
    crypto (Binance and similar: trades 24/7, daily bar closes at
    00:00 UTC). WRONG for equities or any exchange with defined trading
    sessions: those bars close at the session end (e.g. 4pm ET for US
    equities, not midnight UTC), and skip weekends, holidays, and half
    days -- a UTC-midnight rule would silently misjudge a bar as
    "complete" hours before the session actually closed, or as
    "incomplete" for an entire closed weekend. This function is named
    for exactly that assumption on purpose: if Day Shift (or any
    non-crypto, non-continuously-traded market) ever needs this, it
    needs its own market-calendar implementation -- session times,
    holidays, half-days -- not a silent reuse of this one under a
    generic name.
    """
    if isinstance(bar_date, str):
        d = datetime.strptime(bar_date, "%Y-%m-%d").date()
    elif isinstance(bar_date, datetime):
        d = bar_date.date()
    else:
        d = bar_date
    return d < datetime.now(timezone.utc).date()
