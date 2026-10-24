"""Verify exclusions survive orchestration into operator-visible reports."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
import ibkr_history_core as core


@pytest.mark.parametrize('policy,excluded', [('pre_expiry_session', True), ('calendar_expiry', False)])
def test_terminal_windows_and_summary(monkeypatch, tmp_path, policy, excluded):
    c = SimpleNamespace(symbol='ES', localSymbol='ESM6', conId=601,
                        lastTradeDateOrContractMonth='20260618', secType='FUT')

    async def discover(ib, cfg):
        return [c], pd.DataFrame([{'symbol': 'ES', 'status': 'resolved'}])

    async def backfill(ib, row, root, cfg, pacer, budget, progress):
        return {'symbol': c.symbol, 'localSymbol': c.localSymbol,
                'status': 'complete', 'requests': 0, 'new_rows': 0}

    monkeypatch.setattr(core, 'discover_quarterly_contracts', discover)
    monkeypatch.setattr(core, 'utc_now', lambda: datetime(2026, 10, 24, tzinfo=timezone.utc))
    monkeypatch.setattr(core, 'backfill_one_contract', backfill)
    monkeypatch.setattr(core, 'reports_for_contract', lambda folder: {})
    cfg = core.Settings(symbols=('ES',), stocks=(), smoke_test=False,
                        output_dir=str(tmp_path), show_progress=False,
                        expired_future_end_policy=policy)
    ib = SimpleNamespace(isConnected=lambda: True)
    _, result = asyncio.run(core.run_backfill(ib, cfg))
    terminal = pd.read_csv(tmp_path / 'excluded_expiry_terminal_windows.csv')
    assert len(terminal) == int(excluded)
    assert result.iloc[0].coverage_notice == (
        'pre_expiry_session_EXCLUDES_FINAL_SESSION' if excluded else '')
    if excluded:
        assert pd.Timestamp(terminal.iloc[0].terminal_excluded_start_utc) == pd.Timestamp('2026-06-17T21:00:00Z')
        assert pd.Timestamp(terminal.iloc[0].terminal_excluded_end_utc) == pd.Timestamp('2026-06-19T00:00:00Z')
    summary = pd.read_csv(tmp_path / 'run_summary.csv').fillna('')
    assert summary.iloc[0].coverage_notice == result.iloc[0].coverage_notice


def test_real_parquet_round_trip_preserves_utc_and_contract(tmp_path):
    pytest.importorskip('pyarrow')
    frame = pd.DataFrame({'timestamp_utc': pd.to_datetime(['2026-06-17T20:59:00Z']),
                          'conId': [601], 'localSymbol': ['ESM6']})
    path = tmp_path / 'synthetic.parquet'
    frame.to_parquet(path, index=False)
    pd.testing.assert_frame_equal(pd.read_parquet(path), frame)
