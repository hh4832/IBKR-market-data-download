"""Offline tests. Real TWS access is not required. Run: python -m pytest -q
Uses pandas pickle in place of Parquet when a local test Python lacks pyarrow.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import ibkr_history_core as m

UTC = timezone.utc

@dataclass
class DummyContract:
    symbol: str
    localSymbol: str
    lastTradeDateOrContractMonth: str
    conId: int
    exchange: str = "CME"


def make_c(symbol="NQ", ym="20261218", num=99):
    return DummyContract(symbol, symbol + ym[-4:], ym, num)


def fake_frame(c, end):
    return pd.DataFrame([dict(timestamp_utc=pd.Timestamp(end - timedelta(minutes=1)),
           open=10., high=12., low=9., close=11., volume=100., average=10.5, barCount=5,
           symbol=c.symbol, localSymbol=c.localSymbol, expiry=c.lastTradeDateOrContractMonth,
           conId=c.conId, exchange=c.exchange, bar_size="1 min", what_to_show="TRADES",
           use_rth=False)])


def test_quarter_generation_and_friday():
    assert m.third_friday(2026, 12).isoformat() == '2026-12-18'
    q = m.candidate_quarter_months(datetime(2026, 10, 18, tzinfo=UTC), 2)
    assert '202412' in q and '202703' in q
    assert '202610' not in q


def test_roll_window_overlap():
    cs = [make_c('NQ', '20260918', 1), make_c('NQ', '20261218', 2)]
    cfg = m.Settings(symbols=('NQ',), coverage_mode='roll_window')
    plan = m.build_download_plan(cs, cfg, datetime(2026, 10, 18, tzinfo=UTC))
    assert plan.iloc[1].start_utc == datetime(2026, 9, 4, tzinfo=UTC)
    assert plan.iloc[1].end_utc == datetime(2026, 10, 16, 21, tzinfo=UTC)


def test_market_session_gap_classification():
    # Oct 15 2026: 16:00 Chicago = 21:00 UTC (CDT), 17:00 = 22:00 UTC.
    dt = pd.to_datetime(['2026-10-15T20:59:00Z', '2026-10-15T22:00:00Z'], utc=True)
    sample = pd.DataFrame({'timestamp_utc': dt})
    gaps = m.gap_report(sample)
    assert len(gaps) == 1
    assert gaps.iloc[0].gap_minutes == 61
    assert gaps.iloc[0].likely_trading_minutes == 0
    assert gaps.iloc[0].classification == 'closed_session_candidate'
    # An unexplained 3-minute hole mid-session should be flagged.
    sample2 = pd.DataFrame({'timestamp_utc': pd.to_datetime(
        ['2026-10-15T18:00:00Z', '2026-10-15T18:04:00Z'], utc=True)})
    suspect = m.gap_report(sample2)
    assert suspect.iloc[0].classification == 'possible_missing_trading_bars_check_holidays'


def test_validation():
    c = make_c()
    end = datetime(2026, 10, 16, tzinfo=UTC)
    df = fake_frame(c, end)
    assert m.validate_frame(df)['invalid_ohlc'] == 0
    assert m.validate_frame(df)['negative_volume'] == 0
    df.loc[0, 'high'] = 8.
    assert m.validate_frame(df)['invalid_ohlc'] == 1


def test_unsafe_sync_paths(tmp_path):
    a = tmp_path / 'root'
    a.mkdir()
    nested = a / 'sub'
    nested.mkdir()
    with pytest.raises(ValueError):
        m.sync_tree_to_google_drive(a, nested)


def test_checkpoint_and_resume_offline(monkeypatch, tmp_path):
    # Transparent offline Parquet test shim: stores pickles at .parquet paths,
    # so checkpoint, file discovery, merge, and validation can be tested.
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', lambda self, path, index=False: self.to_pickle(path))
    monkeypatch.setattr(pd, 'read_parquet', lambda path: pd.read_pickle(path))
    c = make_c()
    config = m.Settings(symbols=('NQ',), output_dir=str(tmp_path), smoke_test=True,
                        smoke_requests_per_contract=1, min_interval_seconds=0.0)
    row = pd.Series(dict(contract=c, start_utc=datetime(2026,10,14,tzinfo=UTC),
                         end_utc=datetime(2026,10,16,tzinfo=UTC)))
    class DummyIB:
        def isConnected(self): return True
    async def requester(ib, contract, end, pacer, settings):
        return fake_frame(contract, end), [], 1
    monkeypatch.setattr(m, 'request_one_day', requester)
    pacer = m.HistoryPacer(0.0, 1000)
    async def go():
        first = await m.backfill_one_contract(DummyIB(),row,tmp_path,config,pacer)
        second = await m.backfill_one_contract(DummyIB(),row,tmp_path,config,pacer)
        return first,second
    first,second = asyncio.run(go())
    assert first['status'] == 'paused_smoke_limit'
    assert second['status'] == 'complete'
    folder = m.contract_folder(tmp_path, c)
    df = m.load_contract_data(folder)
    assert len(df) == 2
    checkpoint = m.json.loads((folder / 'checkpoint.json').read_text())
    assert checkpoint['next_cursor_utc'].startswith('2026-10-14T00:00:00')
    assert len(list((folder / 'chunks').glob('*.parquet'))) == 2
    assert m.reports_for_contract(folder)['rows'] == 2


def test_incremental_drive_folder_copy(tmp_path):
    local, remote = tmp_path / 'local', tmp_path / 'cloud'
    local.mkdir(); remote.mkdir()
    (local / 'raw').mkdir()
    (local / 'raw' / 'a.txt').write_text('abc')
    res = m.sync_tree_to_google_drive(local, remote)
    assert res['files_copied'] == 1
    assert res['cloud_upload_verified'] is False
    assert (remote / 'raw' / 'a.txt').read_text() == 'abc'
    res2 = m.sync_tree_to_google_drive(local, remote)
    assert res2['files_copied'] == 0


def test_equity_extended_hours_gap_classification():
    # 2026 Oct 15 23:59 UTC = 19:59 ET; next 08:00 UTC = 04:00 ET.
    ok = pd.DataFrame({'timestamp_utc': pd.to_datetime(
        ['2026-10-15T23:59:00Z', '2026-10-16T08:00:00Z'], utc=True)})
    out = m.gap_report(ok, sec_type='STK')
    assert out.iloc[0].classification == 'closed_session_candidate'
    bad = pd.DataFrame({'timestamp_utc': pd.to_datetime(
        ['2026-10-16T14:00:00Z', '2026-10-16T14:04:00Z'], utc=True)})
    out2 = m.gap_report(bad, sec_type='STK')
    assert out2.iloc[0].likely_trading_minutes == 3
    assert out2.iloc[0].classification == 'possible_missing_trading_bars_check_holidays'


def test_request_budget_rollover_and_resume_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', lambda self, path, index=False: self.to_pickle(path))
    monkeypatch.setattr(pd, 'read_parquet', lambda path: pd.read_pickle(path))
    c = make_c()
    config = m.Settings(symbols=('NQ',), stocks=(), output_dir=str(tmp_path), smoke_test=False,
                        max_requests_per_contract_per_run=5, min_interval_seconds=0.0)
    row = pd.Series(dict(contract=c, start_utc=datetime(2026,10,12,tzinfo=UTC),
                         end_utc=datetime(2026,10,17,tzinfo=UTC)))
    class DummyIB:
        def isConnected(self): return True
    async def requester(ib, contract, end, pacer, settings):
        return fake_frame(contract, end), [], 1
    monkeypatch.setattr(m, 'request_one_day', requester)
    async def go():
        budget1 = m.RequestBudget(2)
        first = await m.backfill_one_contract(DummyIB(), row, tmp_path, config,
                                              m.HistoryPacer(0.0, 1000), budget=budget1)
        budget2 = m.RequestBudget(10)
        second = await m.backfill_one_contract(DummyIB(), row, tmp_path, config,
                                               m.HistoryPacer(0.0, 1000), budget=budget2)
        return first, second
    first, second = asyncio.run(go())
    assert first['status'] == 'paused_budget' and first['requests'] == 2
    assert second['status'] == 'complete' and second['requests'] == 3
    assert len(list((m.contract_folder(tmp_path,c)/'chunks').glob('*.parquet'))) == 5


def test_cash_history_plan_head_start(monkeypatch):
    c = DummyContract('TSM', 'TSM', '', 6223250, 'SMART')
    async def fake_head(ib, contract): return datetime(2010, 4, 5, 14, 3, tzinfo=UTC)
    monkeypatch.setattr(m, 'head_timestamp_utc', fake_head)
    plan = asyncio.run(m.build_cash_plan(object(), [c], m.Settings(smoke_test=False),
                                          datetime(2026,10,18,tzinfo=UTC)))
    assert plan.iloc[0].start_utc == datetime(2016,10,18,tzinfo=UTC)
    assert plan.iloc[0].symbol == 'TSM'


def test_settings_indices_explicitly_opt_in():
    x = m.Settings()
    assert x.indices == ()
    assert x.stocks == ('TSM', 'SOXX')
    with pytest.raises(ValueError):
        m.Settings(stocks=('SPY',)).validate()


def test_multisymbol_smoke_run_end_to_end_mocked(monkeypatch, tmp_path):
    # Proves four symbols, independent saved paths, progress-compatible run summary.
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', lambda self, path, index=False: self.to_pickle(path))
    monkeypatch.setattr(pd, 'read_parquet', lambda path: pd.read_pickle(path))
    futures = [make_c('NQ', '20261218', 101), make_c('ES', '20261218', 102)]
    equities = [DummyContract('TSM', 'TSM', '', 103, 'SMART'),
                DummyContract('SOXX', 'SOXX', '', 104, 'SMART')]
    async def discover_f(ib, cfg): return futures, pd.DataFrame([{'symbol':'NQ','status':'resolved'}])
    async def discover_e(ib, cfg): return equities, pd.DataFrame([{'symbol':'TSM','status':'resolved'}])
    async def build_e(ib, cs, cfg, head_cache_path=None):
        return pd.DataFrame([dict(symbol=c.symbol,localSymbol=c.localSymbol,conId=c.conId,
            expiry='',contract=c,start_utc=datetime(2026,10,13,tzinfo=UTC),
            end_utc=datetime(2026,10,16,tzinfo=UTC),eligible=True) for c in cs])
    def build_f(cs, cfg, now=None):
        return pd.DataFrame([dict(symbol=c.symbol,localSymbol=c.localSymbol,conId=c.conId,
            expiry='2026-12-18',contract=c,start_utc=datetime(2026,10,13,tzinfo=UTC),
            end_utc=datetime(2026,10,16,tzinfo=UTC),eligible=True) for c in cs])
    async def requester(ib, c, end, pacer, cfg): return fake_frame(c, end), [], 1
    monkeypatch.setattr(m,'discover_quarterly_contracts',discover_f)
    monkeypatch.setattr(m,'discover_cash_contracts',discover_e)
    monkeypatch.setattr(m,'build_cash_plan',build_e)
    monkeypatch.setattr(m,'build_download_plan',build_f)
    monkeypatch.setattr(m,'request_one_day',requester)
    class IB:
        def isConnected(self): return True
    cfg = m.Settings(output_dir=str(tmp_path),smoke_test=True,smoke_requests_per_contract=1,
                     max_requests_per_run=10,show_progress=False,min_interval_seconds=0.0)
    planned,results=asyncio.run(m.run_backfill(IB(),cfg))
    assert set(results.symbol)=={'NQ','ES','TSM','SOXX'}
    assert sum(results.requests)==4
    for c in futures + equities:
        assert len(list((m.contract_folder(tmp_path,c)/'chunks').glob('*.parquet')))==1
    assert (tmp_path/'run_summary.csv').exists()


def test_smoke_cash_plan_does_not_require_head_timestamp(monkeypatch):
    """Regression: a missing SOXX head timestamp must not block smoke."""
    c = DummyContract('SOXX', 'SOXX', '', 12658194, 'SMART')
    async def bad_head(ib, contract):
        raise AssertionError('smoke should not query reqHeadTimeStampAsync')
    monkeypatch.setattr(m, 'head_timestamp_utc', bad_head)
    cfg = m.Settings(smoke_test=True)
    plan = asyncio.run(m.build_cash_plan(object(), [c], cfg,
                                          datetime(2026, 10, 18, 9, tzinfo=UTC)))
    assert plan.iloc[0].symbol == 'SOXX'
    assert plan.iloc[0].head_source == 'smoke_not_queried'
    assert plan.iloc[0].end_utc == datetime(2026, 10, 17, 0, tzinfo=UTC)


def test_full_cash_plan_missing_head_falls_back_explicitly(monkeypatch):
    c = DummyContract('SOXX', 'SOXX', '', 12658194, 'SMART')
    async def missing_head(ib, contract):
        raise RuntimeError('no timestamp')
    monkeypatch.setattr(m, 'head_timestamp_utc', missing_head)
    cfg = m.Settings(smoke_test=False, equity_fallback_year=1990)
    plan = asyncio.run(m.build_cash_plan(object(), [c], cfg,
                                          datetime(2026, 10, 18, tzinfo=UTC)))
    assert plan.iloc[0].start_utc == datetime(2016, 10, 18, tzinfo=UTC)
    assert plan.iloc[0].head_source == 'fallback_floor_UNVERIFIED'
    assert 'no timestamp' in plan.iloc[0].head_error


def test_full_cash_plan_strict_head_fails(monkeypatch):
    c = DummyContract('SOXX', 'SOXX', '', 12658194, 'SMART')
    async def missing_head(ib, contract):
        raise RuntimeError('no timestamp')
    monkeypatch.setattr(m, 'head_timestamp_utc', missing_head)
    cfg = m.Settings(smoke_test=False, equity_head_required=True)
    with pytest.raises(RuntimeError, match='Head timestamp lookup failed'):
        asyncio.run(m.build_cash_plan(object(), [c], cfg,
                                      datetime(2026, 10, 18, tzinfo=UTC)))


def test_head_timestamp_missing_is_explicit_error():
    c = DummyContract('SOXX', 'SOXX', '', 12658194, 'SMART')
    class DummyIB:
        async def reqHeadTimeStampAsync(self, contract, **kwargs):
            return None
    with pytest.raises(RuntimeError, match='did not supply'):
        asyncio.run(m.head_timestamp_utc(DummyIB(), c))


def test_most_recent_equity_close_dst_friday_and_weekend():
    # Sun Oct 18 2026 after EDT close should use Fri Oct 16 20 ET == Sat 00 UTC.
    close = m.most_recent_equity_close_utc(datetime(2026, 10, 18, 9, tzinfo=UTC))
    assert close == datetime(2026, 10, 17, 0, tzinfo=UTC)
    # Monday ET morning before 20 ET should also resolve to Friday.
    close2 = m.most_recent_equity_close_utc(datetime(2026, 10, 19, 14, tzinfo=UTC))
    assert close2 == close


def test_full_cash_head_verified_retained(monkeypatch):
    c = DummyContract('TSM', 'TSM', '', 6223250, 'SMART')
    async def real_head(ib, contract):
        return datetime(1997, 12, 1, 4, tzinfo=UTC)
    monkeypatch.setattr(m, 'head_timestamp_utc', real_head)
    cfg = m.Settings(smoke_test=False)
    plan = asyncio.run(m.build_cash_plan(object(), [c], cfg,
                                          datetime(2026, 10, 18, tzinfo=UTC)))
    assert plan.iloc[0].head_source == 'ibkr_head_timestamp'
    assert plan.iloc[0].start_utc == datetime(2016, 10, 18, tzinfo=UTC)


def test_most_recent_futures_trading_end_weekend_and_maintenance():
    # Sun Oct 18 09:00 UTC == Sunday 04:00 CDT. Prior Fri 16:00 CDT is Fri 21:00 UTC.
    assert m.most_recent_futures_trading_end_utc(datetime(2026,10,18,9,tzinfo=UTC)) == \
           datetime(2026,10,16,21,tzinfo=UTC)
    # Mon Oct 19 16:30 CDT = Oct 19 21:30 UTC, maintenance ends 22:00 UTC.
    assert m.most_recent_futures_trading_end_utc(datetime(2026,10,19,21,30,tzinfo=UTC)) == \
           datetime(2026,10,19,21,tzinfo=UTC)

# v1.3 regression tests for invalid plans, fairness and conservative fallback.
def test_future_start_after_end_is_explicitly_skipped():
    now = datetime(2026, 10, 18, 9, tzinfo=UTC)
    contracts = [make_c('NQ', '20261218', 101), make_c('NQ', '20270319', 102)]
    plan = m.build_download_plan(contracts, m.Settings(symbols=('NQ',), stocks=()), now)
    good, skipped = m.filter_eligible_plan(plan)
    assert good.localSymbol.tolist() == [contracts[0].localSymbol]
    assert skipped.localSymbol.tolist() == [contracts[1].localSymbol]
    assert (good.start_utc < good.end_utc).all()


def test_fair_symbol_rotation():
    rows=[]
    for sym, n in [('ES', 4), ('NQ', 4), ('SOXX', 1), ('TSM', 1)]:
        for i in range(n):
            rows.append(dict(symbol=sym, end_utc=datetime(2026, 10, 16-i, tzinfo=UTC),
                             marker=f'{sym}-{i}'))
    result=m.fair_contract_order(pd.DataFrame(rows))
    assert result.symbol.iloc[:4].tolist()==['ES','NQ','SOXX','TSM']
    assert result.symbol.iloc[4:6].tolist()==['ES','NQ']
    assert result.loc[result.symbol=='ES','marker'].tolist()==['ES-0','ES-1','ES-2','ES-3']


def test_cash_unverified_history_boundary_is_nonfatal_and_persistent(monkeypatch, tmp_path):
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', lambda self, path, index=False: self.to_pickle(path))
    monkeypatch.setattr(pd, 'read_parquet', lambda path: pd.read_pickle(path))
    c = DummyContract('SOXX', 'SOXX', '', 12658194, 'SMART')
    cfg = m.Settings(symbols=(),stocks=('SOXX',),output_dir=str(tmp_path),smoke_test=False,
                     equity_empty_weekdays_to_stop=3,max_requests_per_contract_per_run=25)
    folder=m.contract_folder(tmp_path,c)
    m.write_chunk(folder/'chunks'/'seed.parquet',fake_frame(c,datetime(2026,10,16,tzinfo=UTC)))
    row=pd.Series(dict(contract=c,start_utc=datetime(2026,10,1,tzinfo=UTC),
                       end_utc=datetime(2026,10,16,tzinfo=UTC),head_source='fallback_floor_UNVERIFIED'))
    class IB:
        def isConnected(self): return True
    async def no_data(ib,c,end,pacer,cfg):
        return pd.DataFrame(columns=m.BAR_COLUMNS),[],1
    monkeypatch.setattr(m,'request_one_day',no_data)
    async def go():
        a=await m.backfill_one_contract(IB(),row,tmp_path,cfg,m.HistoryPacer(0.,1000))
        b=await m.backfill_one_contract(IB(),row,tmp_path,cfg,m.HistoryPacer(0.,1000))
        return a,b
    first,second=asyncio.run(go())
    assert first['status']=='boundary_unverified_review'
    assert first['requests']==3
    assert second['status']=='boundary_unverified_already_stopped'
    assert second['requests']==0
    evidence=m.json.loads((folder/'unverified_history_boundary.json').read_text(encoding='utf-8'))
    assert evidence['warning'].startswith('Heuristic stop')
    assert len(list((folder/'chunks').glob('*.parquet')))==1


def test_verified_head_does_not_trigger_heuristic_stop(monkeypatch, tmp_path):
    c = DummyContract('TSM', 'TSM', '', 6223250, 'SMART')
    cfg = m.Settings(symbols=(),stocks=('TSM',),smoke_test=False,
                     max_requests_per_contract_per_run=3,equity_empty_weekdays_to_stop=3)
    row=pd.Series(dict(contract=c,start_utc=datetime(2026,10,1,tzinfo=UTC),
                       end_utc=datetime(2026,10,16,tzinfo=UTC),head_source='ibkr_head_timestamp'))
    class IB:
        def isConnected(self): return True
    async def no_data(ib,c,end,pacer,cfg):
        return pd.DataFrame(columns=m.BAR_COLUMNS),[],1
    monkeypatch.setattr(m,'request_one_day',no_data)
    result=asyncio.run(m.backfill_one_contract(IB(),row,tmp_path,cfg,m.HistoryPacer(0.,1000)))
    assert result['status']=='paused_contract_slice'
    assert not (m.contract_folder(tmp_path,c)/'unverified_history_boundary.json').exists()


def test_head_lookup_cached_after_failure(monkeypatch, tmp_path):
    c=DummyContract('SOXX','SOXX','',12658194,'SMART')
    calls=[]
    async def fail_head(ib,c):
        calls.append(c.symbol)
        raise RuntimeError('temporarily unavailable')
    monkeypatch.setattr(m,'head_timestamp_utc',fail_head)
    cfg=m.Settings(smoke_test=False,equity_fallback_year=1990)
    cache=tmp_path/'head_timestamp_cache.json'
    at=datetime(2026,10,18,tzinfo=UTC)
    async def go():
        a=await m.build_cash_plan(object(),[c],cfg,at,head_cache_path=cache)
        b=await m.build_cash_plan(object(),[c],cfg,at+timedelta(days=1),head_cache_path=cache)
        return a,b
    a,b=asyncio.run(go())
    assert calls==['SOXX']
    assert a.iloc[0].head_source==b.iloc[0].head_source=='fallback_floor_UNVERIFIED'
    assert cache.exists()

# v1.4 regressions: avoid end-of-contract non-trading requests,
# prevent needless weekend API calls, and allow unaffected contracts to run.

def test_expired_contract_end_never_two_days_after_last_trade():
    c = make_c('ES', '20260618', 123)
    cfg = m.Settings(symbols=('ES',), stocks=())
    p = m.build_download_plan([c], cfg, datetime(2026,10,18,tzinfo=UTC))
    assert p.iloc[0].end_utc == datetime(2026,6,17,21,tzinfo=UTC)
    assert p.iloc[0].terminal_policy == 'pre_expiry_session_EXCLUDES_FINAL_SESSION'
    assert p.iloc[0].terminal_excluded_start_utc == datetime(2026,6,17,21,tzinfo=UTC)
    assert p.iloc[0].terminal_excluded_end_utc == datetime(2026,6,19,tzinfo=UTC)
    assert p.iloc[0].eligible


def test_weekly_full_closure_without_guessing_partial_open():
    c = make_c('ES','20261218')
    # Saturday 00 UTC -> Sunday 00 UTC: no CME weekly trading.
    assert m.is_verified_weekly_closed_window(c, datetime(2026,10,18,tzinfo=UTC))
    # Sunday 00 UTC -> Monday 00 UTC includes Sunday Globex reopen.
    assert not m.is_verified_weekly_closed_window(c, datetime(2026,10,19,tzinfo=UTC))
    # Friday 00 UTC -> Saturday 00 UTC includes Friday session.
    assert not m.is_verified_weekly_closed_window(c, datetime(2026,10,17,tzinfo=UTC))
    stk = DummyContract('TSM','TSM','',6223250,'SMART')
    stk.secType = 'STK'
    assert m.is_verified_weekly_closed_window(stk, datetime(2026,10,19,tzinfo=UTC))


def test_weekly_closed_window_skips_api_and_checkpoints(monkeypatch,tmp_path):
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', lambda self,path,index=False: self.to_pickle(path))
    monkeypatch.setattr(pd,'read_parquet',lambda path: pd.read_pickle(path))
    c = make_c('ES','20261218')
    cfg = m.Settings(symbols=('ES',), stocks=(), output_dir=str(tmp_path),
                     smoke_test=False, max_requests_per_contract_per_run=2,
                     min_interval_seconds=0, show_progress=False)
    row = pd.Series(dict(contract=c, start_utc=datetime(2026,10,17,tzinfo=UTC),
                         end_utc=datetime(2026,10,19,tzinfo=UTC)))
    calls=[]
    async def requester(ib,c,end,pacer,settings):
        calls.append(end)
        return fake_frame(c,end),[],1
    monkeypatch.setattr(m,'request_one_day',requester)
    class IB:
        def isConnected(self):return True
    b=m.RequestBudget(2)
    res=asyncio.run(m.backfill_one_contract(IB(),row,tmp_path,cfg,m.HistoryPacer(0,100),budget=b))
    assert res['status']=='complete'
    assert res['weekly_closed_skips']==1
    assert res['requests']==1
    assert calls==[datetime(2026,10,19,tzinfo=UTC)]
    assert b.used==1
    checkpoint=m.json.loads((m.contract_folder(tmp_path,c)/'checkpoint.json').read_text())
    assert checkpoint['next_cursor_utc'].startswith('2026-10-17T00:00:00')


def test_failed_contract_does_not_block_other_symbols(monkeypatch,tmp_path):
    monkeypatch.setattr(pd.DataFrame,'to_parquet',lambda self,path,index=False:self.to_pickle(path))
    monkeypatch.setattr(pd,'read_parquet',lambda path:pd.read_pickle(path))
    bad = make_c('ES','20260618',201)
    good = make_c('NQ','20260618',202)
    # Mock discoveries/plans so the test controls the exact failing day.
    async def discover_f(ib,cfg):return [bad,good],pd.DataFrame([{'symbol':'ES','status':'resolved'}])
    def plan_f(cs,cfg,now=None):
        return pd.DataFrame([dict(symbol=c.symbol,localSymbol=c.localSymbol,conId=c.conId,
           expiry='2026-06-18',contract=c,start_utc=datetime(2026,6,17,tzinfo=UTC),
           end_utc=datetime(2026,6,18,tzinfo=UTC),eligible=True) for c in cs])
    async def requester(ib,c,end,pacer,settings):
        if c.symbol=='ES':raise RuntimeError('Ambiguous empty/timeout after retries for ESM6')
        return fake_frame(c,end),[],1
    monkeypatch.setattr(m,'discover_quarterly_contracts',discover_f)
    monkeypatch.setattr(m,'build_download_plan',plan_f)
    monkeypatch.setattr(m,'request_one_day',requester)
    class IB:
        def isConnected(self):return True
    cfg=m.Settings(symbols=('ES','NQ'),stocks=(),smoke_test=False,output_dir=str(tmp_path),
                   min_interval_seconds=0,show_progress=False)
    _,r=asyncio.run(m.run_backfill(IB(),cfg))
    assert r.set_index('symbol').loc['ES','status']=='failed_retryable'
    assert r.set_index('symbol').loc['NQ','status']=='complete'
    assert (tmp_path/'failures.jsonl').exists()
    assert len(list((m.contract_folder(tmp_path,good)/'chunks').glob('*.parquet')))==1
    assert not (m.contract_folder(tmp_path,bad)/'checkpoint.json').exists()


# v1.5: terminal settlement guard and decade-only equity planning.
def test_nqm6_expiry_guard_same_as_esm6():
    cfg = m.Settings(symbols=('NQ','ES'),stocks=(),smoke_test=False)
    cs=[make_c('NQ','20260618',600), make_c('ES','20260618',601)]
    plan=m.build_download_plan(cs,cfg,datetime(2026,10,18,tzinfo=UTC))
    assert len(plan)==2
    assert (plan.end_utc==datetime(2026,6,17,21,tzinfo=UTC)).all()
    assert (plan.terminal_policy=='pre_expiry_session_EXCLUDES_FINAL_SESSION').all()

def test_expired_regular_friday_guards_final_session_dst():
    cfg=m.Settings(symbols=('ES',),stocks=())
    c=make_c('ES','20260320',603)
    row=m.build_download_plan([c],cfg,datetime(2026,10,18,tzinfo=UTC)).iloc[0]
    # Thursday 2026-03-19 Chicago still in CDT => 21 UTC.
    assert row.end_utc==datetime(2026,3,19,21,tzinfo=UTC)

def test_expiry_policy_revert_opt_in():
    cfg=m.Settings(symbols=('ES',),stocks=(),expired_future_end_policy='calendar_expiry')
    c=make_c('ES','20260618',605)
    row=m.build_download_plan([c],cfg,datetime(2026,10,18,tzinfo=UTC)).iloc[0]
    assert row.end_utc==datetime(2026,6,19,tzinfo=UTC)
    assert row.terminal_policy=='calendar_expiry_or_unexpired'

def test_equity_lookback_with_verified_head(monkeypatch):
    c=DummyContract('TSM','TSM','',6223250,'SMART')
    async def head(ib,c):return datetime(1997,12,1,tzinfo=UTC)
    monkeypatch.setattr(m,'head_timestamp_utc',head)
    cfg=m.Settings(smoke_test=False,equity_lookback_years=10)
    r=asyncio.run(m.build_cash_plan(object(),[c],cfg,datetime(2026,10,18,tzinfo=UTC)))
    assert r.iloc[0].start_utc==datetime(2016,10,18,tzinfo=UTC)
    assert r.iloc[0].head_source=='ibkr_head_timestamp'

def test_equity_lookback_none_unlimited_if_head_known(monkeypatch):
    c=DummyContract('TSM','TSM','',6223250,'SMART')
    async def head(ib,c):return datetime(1997,12,1,tzinfo=UTC)
    monkeypatch.setattr(m,'head_timestamp_utc',head)
    cfg=m.Settings(smoke_test=False,equity_lookback_years=None)
    r=asyncio.run(m.build_cash_plan(object(),[c],cfg,datetime(2026,10,18,tzinfo=UTC)))
    assert r.iloc[0].start_utc==datetime(1997,12,1,tzinfo=UTC)

def test_equity_leap_day_floor():
    assert m.equity_lookback_floor(datetime(2024,2,29,tzinfo=UTC),1)==datetime(2023,2,28,tzinfo=UTC)

def test_v15_caps_existing_checkpoint_without_wiping(monkeypatch, tmp_path):
    monkeypatch.setattr(pd.DataFrame,'to_parquet',lambda self,path,index=False:self.to_pickle(path))
    monkeypatch.setattr(pd,'read_parquet',lambda path:pd.read_pickle(path))
    c=make_c('ES','20260618',801)
    cfg=m.Settings(symbols=('ES',),stocks=(),output_dir=str(tmp_path),smoke_test=False,
      min_interval_seconds=0,max_requests_per_contract_per_run=1,show_progress=False)
    folder=m.contract_folder(tmp_path,c)
    folder.mkdir(parents=True)
    checkpoint=folder/'checkpoint.json'
    m.atomic_json(checkpoint,dict(next_cursor_utc='2026-06-19T00:00:00+00:00',coverage_mode='roll_window'))
    row=m.build_download_plan([c],cfg,datetime(2026,10,18,tzinfo=UTC)).iloc[0]
    calls=[]
    async def requester(ib,c,end,pacer,settings):
        calls.append(end)
        return fake_frame(c,end),[],1
    monkeypatch.setattr(m,'request_one_day',requester)
    class IB:
        def isConnected(self):return True
    res=asyncio.run(m.backfill_one_contract(IB(),row,tmp_path,cfg,m.HistoryPacer(0,100)))
    assert calls==[datetime(2026,6,17,21,tzinfo=UTC)]
    assert res['requests']==1
    assert len(list((folder/'chunks').glob('*.parquet')))==1
    assert m.parse_utc(m.json.loads(checkpoint.read_text())['next_cursor_utc'])==datetime(2026,6,16,21,tzinfo=UTC)

def test_settings_guard_validation():
    with pytest.raises(ValueError):m.Settings(equity_lookback_years=0).validate()
    with pytest.raises(ValueError):m.Settings(expired_future_end_policy='silent_skip').validate()
