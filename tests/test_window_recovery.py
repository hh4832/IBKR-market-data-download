"""Actual regression shapes: 60 winter / 120 summer bars at a midnight endpoint."""
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import pandas as pd
import pytest
import ibkr_history_core as core
import ibkr_history_recovery as recovery
from ibkr_window_quality import assess_window, regular_minutes, IncompleteWindowError


class Event:
    def __iadd__(self, fn): return self
    def __isub__(self, fn): return self


class Pacer:
    def __init__(self): self.calls=0
    async def wait(self): self.calls+=1


def contract():
    return SimpleNamespace(symbol='NQ',localSymbol='NQH5',conId=666754605,
                           secType='FUT',exchange='CME',lastTradeDateOrContractMonth='20250321')


def frame(ts):
    c=contract()
    return pd.DataFrame([dict(timestamp_utc=t,conId=c.conId,symbol=c.symbol,
                             open=1.,high=2.,low=.5,close=1.5,volume=5.) for t in ts])


@pytest.mark.parametrize('end,start', [('2024-12-11T00:00Z','2024-12-10T23:00Z'),
                                      ('2025-03-20T00:00Z','2025-03-19T22:00Z')])
def test_short_nonempty_day_rejected_without_touching_checkpoint(monkeypatch,tmp_path,end,start):
    end=pd.Timestamp(end).to_pydatetime();partial=frame(pd.date_range(start,pd.Timestamp(end),inclusive='left',freq='min'))
    class IB:
        errorEvent=Event()
        def isConnected(self): return True
        async def reqHistoricalDataAsync(self,*args,**kw):
            assert kw['durationStr']=='86400 S'
            return ['nonempty']
    monkeypatch.setattr(core,'bars_to_frame',lambda bars,c:partial)
    folder=core.contract_folder(tmp_path,contract());folder.mkdir(parents=True)
    checkpoint=folder/'checkpoint.json';checkpoint.write_text(json.dumps(dict(next_cursor_utc=end.isoformat())))
    before=checkpoint.read_bytes()
    row={'contract':contract(),'start_utc':end-timedelta(days=2),'end_utc':end}
    with pytest.raises(IncompleteWindowError):
        asyncio.run(core.backfill_one_contract(IB(),row,tmp_path,core.Settings(),Pacer()))
    assert checkpoint.read_bytes()==before
    assert not list(folder.rglob('*.parquet'))


def test_sunday_sixty_bars_are_not_a_short_full_day():
    end=pd.Timestamp('2024-12-09T00:00Z')
    ts=regular_minutes(end-pd.Timedelta(days=1),end)
    assert len(ts)==60
    assert not assess_window(frame(ts),end-pd.Timedelta(days=1),end)['suspicious']


def test_full_regular_day_passes_but_internal_gap_fails():
    end=pd.Timestamp('2025-03-20T00:00Z');start=end-pd.Timedelta(days=1)
    ts=regular_minutes(start,end)
    assert len(ts)==1380
    assert not assess_window(frame(ts),start,end)['suspicious']
    assert assess_window(frame(ts.delete(slice(200,400))),start,end)['suspicious']


def test_holiday_short_session_is_review_not_false_complete():
    end=pd.Timestamp('2025-12-26T00:00Z')
    result=assess_window(frame(pd.date_range('2025-12-25T23:00Z',periods=60,freq='min')),
                         end-pd.Timedelta(days=1),end)
    assert result['suspicious']
    assert 'CHECK_HOLIDAYS' in result['classification']


def test_explicit_no_data_open_futures_does_not_advance(monkeypatch):
    class CaptureEvent:
        def __iadd__(self,fn):self.handler=fn;return self
        def __isub__(self,fn):return self
    class IB:
        errorEvent=CaptureEvent()
        async def reqHistoricalDataAsync(self,*args,**kw):
            self.errorEvent.handler(1,162,'HMDS query returned no data',contract())
            return []
    monkeypatch.setattr(core,'bars_to_frame',lambda bars,c:pd.DataFrame(columns=['timestamp_utc']))
    with pytest.raises(IncompleteWindowError,match='Unverified no-data'):
        asyncio.run(core.request_one_day(IB(),contract(),pd.Timestamp('2024-12-09T00:00Z').to_pydatetime(),
                                         Pacer(),core.Settings()))
    result=assess_window(pd.DataFrame(columns=['timestamp_utc']),
                         '2024-12-08T00:00Z','2024-12-09T00:00Z')
    assert result['suspicious'] and result['expected_standard_minutes']==60


def test_plan_path_traversal_is_rejected(dataset):
    root,folder,ts=dataset;plan=recovery.build_plan(root)
    plan['windows'][0]['id']='../../outside'
    with pytest.raises(ValueError,match='Unsafe'):recovery._validate_windows(plan)


@pytest.fixture
def dataset(tmp_path):
    root=tmp_path/'original';folder=root/'raw'/'NQ'/'NQH5_666754605';(folder/'chunks').mkdir(parents=True)
    start=pd.Timestamp('2025-03-19T00:00Z');end=pd.Timestamp('2025-03-20T00:00Z')
    pd.DataFrame([dict(symbol='NQ',localSymbol='NQH5',conId=666754605,start_utc=start.isoformat(),end_utc=end.isoformat())]).to_csv(root/'download_plan.csv',index=False)
    ts=regular_minutes(start,end)
    frame(ts[-120:]).to_parquet(folder/'chunks'/'end_old.parquet',index=False)
    (folder/'checkpoint.json').write_text('{"next_cursor_utc":"2024-01-01T00:00:00+00:00"}')
    return root,folder,ts


def test_plan_fills_holes_before_exhausted_checkpoint_and_splits_bounds(dataset):
    root,folder,ts=dataset
    plan=recovery.build_plan(root)
    assert plan['summary'][0]['missing_candidate_minutes']==1260
    assert sum(w['missing_minutes'] for w in plan['windows'])==1260
    assert all(60<=w['duration_seconds']<=21600 for w in plan['windows'])
    for w in plan['windows']:
        interval=regular_minutes(w['start_utc'],w['end_utc'])
        assert not interval.isin(ts[-120:]).any()
    recovery.verify_source(plan)


def test_preview_never_connects_or_writes_to_recovery(dataset,tmp_path,monkeypatch):
    root,folder,ts=dataset;plan=recovery.build_plan(root)
    path=tmp_path/'plan.json';path.write_text(json.dumps(plan))
    out=tmp_path/'overlay'
    assert recovery.main(['recover','--plan',str(path),'--recovery-root',str(out)])==0
    assert not out.exists()
    with pytest.raises(ValueError):recovery.separate(root,root/'recovery')
    with pytest.raises(ValueError):recovery.separate(root,root.parent/'other'/'..'/root.name)


def test_source_mutation_invalidates_plan(dataset):
    root,folder,ts=dataset;plan=recovery.build_plan(root)
    (folder/'checkpoint.json').write_text('{}')
    with pytest.raises(RuntimeError,match='Source changed'):recovery.verify_source(plan)


def test_recovery_cap_and_partial_response_never_touch_original(dataset,tmp_path,monkeypatch):
    import sys,types
    root,folder,ts=dataset;plan=recovery.build_plan(root)
    baseline={str(p):recovery.digest(p) for p in root.rglob('*') if p.is_file()}
    fake=types.ModuleType('ib_async');fake.Contract=lambda **kwargs:SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules,'ib_async',fake)
    calls=[]
    class IB:
        errorEvent=Event()
        def isConnected(self):return True
        async def reqHistoricalDataAsync(self,c,**kw):calls.append(kw);return [1]
    w=plan['windows'][0]
    actual=frame(pd.date_range(w['start_utc'],periods=20,freq='min'))
    monkeypatch.setattr(core,'bars_to_frame',lambda bars,c:actual)
    pacer=Pacer()
    out=asyncio.run(recovery.recover(IB(),plan,tmp_path/'overlay',pacer,1,approve_calendar=True))
    assert len(calls)==pacer.calls==out['requests']==1
    assert calls[0]['durationStr']==f"{w['duration_seconds']} S"
    assert out['results'][0]['status']=='PARTIAL_UNRESOLVED'
    assert baseline=={str(p):recovery.digest(p) for p in root.rglob('*') if p.is_file()}
    merged=recovery.load_with_recovery(folder,Path(out['run_dir'])/'raw'/'NQ'/folder.name)
    assert len(merged)==140
    replanned=recovery.build_plan(root,overlay=Path(out['run_dir']).parent)
    assert replanned['summary'][0]['missing_candidate_minutes']==1240


def test_overlay_overlap_preserves_original_price(dataset,tmp_path):
    root,folder,ts=dataset;extra=tmp_path/'extra';(extra/'chunks').mkdir(parents=True)
    changed=frame(ts[-1:]);changed['close']=999
    changed.to_parquet(extra/'chunks'/'extra.parquet',index=False)
    merged=recovery.load_with_recovery(folder,extra)
    assert merged.iloc[-1]['close']==1.5
