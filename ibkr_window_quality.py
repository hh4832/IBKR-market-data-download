"""Conservative futures window checks, NOT a holiday calendar or completeness certificate."""
from __future__ import annotations

import pandas as pd


def regular_minutes(start, end) -> pd.DatetimeIndex:
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError('Window must be aware and increasing')
    start, end = start.tz_convert('UTC'), end.tz_convert('UTC')
    grid = pd.date_range(start.ceil('min'), end, inclusive='left', freq='min')
    ct = grid.tz_convert('America/Chicago')
    wd, hr = ct.weekday, ct.hour
    mask = ((wd < 4) & (hr != 16)) | ((wd == 4) & (hr < 16)) | ((wd == 6) & (hr >= 17))
    return grid[mask]


def assess_window(frame: pd.DataFrame, start, end) -> dict:
    """Flag severe holes for review; never equate heuristic acceptance with complete data.

    Holidays/halts and thin trading can legitimately trigger this check. Caller must
    stop or quarantine the window rather than silently advancing a resume pointer.
    """
    expected = regular_minutes(start, end)
    ts = pd.DatetimeIndex(pd.to_datetime(frame.get('timestamp_utc', []), utc=True)).unique()
    present = expected.isin(ts)
    longest = run = 0
    previous = None
    for t, exists in zip(expected, present):
        if exists:
            run = 0
        else:
            run = run + 1 if previous is not None and t - previous == pd.Timedelta(minutes=1) else 1
            longest = max(longest, run)
        previous = t
    count = int(present.sum())
    suspicious = bool((len(expected) and count == 0) or
                      (len(expected) >= 180 and (count < len(expected) * .5 or longest >= 180)))
    return dict(expected_standard_minutes=len(expected), observed_standard_minutes=count,
                missing_standard_minutes=len(expected)-count,
                longest_missing_standard_run=longest, suspicious=suspicious,
                classification=('REVIEW_SHORT_OR_GAPPED_RESPONSE_CHECK_HOLIDAYS' if suspicious
                                else 'HEURISTIC_CHECK_PASSED_NOT_COVERAGE_CERTIFIED'),
                first_bar_utc=ts.min().isoformat() if len(ts) else None,
                last_bar_utc=ts.max().isoformat() if len(ts) else None)


class IncompleteWindowError(RuntimeError):
    """Response is unsafe to commit as an entire one-day window."""
