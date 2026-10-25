"""Offline evidence classification. Provider coverage is not exchange-wide completeness."""
from datetime import datetime, timezone
from math import isfinite


def utc(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Evidence timestamps must include a timezone')
    return value.astimezone(timezone.utc)


def assess_forward_ticks(start, end, ticks, *, completed, errors=()):
    """Assess a successful forward TRADES query requested at gap start.

    Caller must retain the exact request (contract, start, TRADES, useRTH=False),
    completion callback, full response and SHA256. This pure function neither
    downloads data nor modifies candles/checkpoints. Do not pass reverse queries.
    A nonempty response beginning at/after end explains IB-feed absence only.
    """
    start, end = utc(start), utc(end)
    if start >= end:
        raise ValueError('Empty or reversed gap')
    result = dict(status='UNRESOLVED', scope='IB_HISTORICAL_TRADES_FEED_ONLY',
                  exchange_wide_no_trade_proven=False, ticks=len(ticks))
    if not completed or errors or not ticks:
        return result
    times = [utc(t['time']) for t in ticks]
    if times != sorted(times) or times[0] < start:
        raise ValueError('Response is not an ordered forward query from gap start')
    for tick in ticks:
        if any(not isfinite(float(tick[k])) or float(tick[k]) <= 0 for k in ('price', 'size')):
            raise ValueError('Invalid trade tick')
    result['first_tick'] = times[0].isoformat()
    result['status'] = ('EXPLAINED_IB_FEED_NO_REPORTED_TRADES' if times[0] >= end
                        else 'TICKS_EXIST_BAR_GAP_REQUIRES_REVIEW')
    return result
