import importlib.util
import json
from pathlib import Path
import sys

import pytest

BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('staged',BASE/'NQ_staged_recovery.py')
staged=importlib.util.module_from_spec(spec);spec.loader.exec_module(staged)
sys.path.insert(0,str(BASE))
import ibkr_history_recovery as engine


def window(start='2024-10-01T00:00:00+00:00',end='2024-10-01T06:00:00+00:00',**kw):
    d=dict(id='1'*24,symbol='NQ',localSymbol='NQZ4',conId=563947733,expiry='2024-12-20',
           start_utc=start,end_utc=end,duration_seconds=21600,missing_minutes=360)
    d.update(kw);return d


def plan(windows,root):
    return dict(source_root=str(root),source_hashes={},summary=[dict(symbol='NQ',localSymbol='NQZ4',conId=563947733,
                expected_standard_minutes=1000,observed_standard_minutes=0,missing_candidate_minutes=1000,windows=len(windows))],windows=windows)


def test_normal_date_selected_and_limit_respected(tmp_path):
    ws=[window(id=str(i)*24) for i in range(1,5)]
    batch,normal,held,_=staged.select_batch(plan(ws,tmp_path),1,tmp_path,2)
    assert len(batch)==2 and len(normal)==4 and not held


@pytest.mark.parametrize('start,end',[
    ('2024-11-28T00:00:00Z','2024-11-28T06:00:00Z'),
    ('2024-11-26T18:00:00Z','2024-11-27T00:00:00Z'),
    ('2025-01-09T00:00:00Z','2025-01-09T06:00:00Z')])
def test_conservative_holiday_buffer(start,end):
    assert staged.review_reason(window(start,end),[])=='HOLIDAY_BUFFER_REVIEW'


def test_small_gap_held():
    assert staged.review_reason(window(missing_minutes=10),[])=='SMALL_GAP_REVIEW'


def test_expiry_boundary_held():
    assert staged.review_reason(window('2024-12-19T00:00:00Z','2024-12-19T06:00:00Z'),[])=='EXPIRY_BOUNDARY_REVIEW'


def test_unknown_calendar_year_held():
    assert staged.review_reason(window('2027-09-20T00:00:00Z','2027-09-20T06:00:00Z'),[])=='CALENDAR_YEAR_NOT_REVIEWED'


def test_partial_interval_held_even_if_replan_id_changes(tmp_path):
    run=tmp_path/'run_test';run.mkdir()
    (run/'results.json').write_text(json.dumps([dict(status='PARTIAL_UNRESOLVED',window=window())]))
    a,b,c,_=staged.select_batch(plan([window(id='a'*24)],tmp_path),1,tmp_path,100)
    assert not a and not b and c[0]['hold_reason']=='PREVIOUS_UNRESOLVED_REVIEW'


def test_successful_old_request_not_marked_failed(tmp_path):
    run=tmp_path/'run_test';run.mkdir()
    (run/'results.json').write_text(json.dumps([dict(status='OBSERVED_ALL_CANDIDATE_MINUTES',window=window())]))
    assert staged.previous_unresolved(tmp_path)==([],[])


def test_bad_results_refuse_safe_resume(tmp_path):
    run=tmp_path/'run_bad';run.mkdir();(run/'results.json').write_text('{')
    with pytest.raises(RuntimeError,match='Cannot safely read'):staged.previous_unresolved(tmp_path)


def test_preview_rebuilds_every_invocation_no_live_calls(monkeypatch,tmp_path):
    source=tmp_path/'source';source.mkdir();overlay=tmp_path/'overlay';overlay.mkdir()
    reports=tmp_path/'reports';calls=[]
    def build(src,symbols,over):
        calls.append(over)
        return plan([window()],source)
    monkeypatch.setattr(engine,'build_plan',build)
    async def forbidden(*a,**kw):raise AssertionError('Preview cannot execute')
    monkeypatch.setattr(staged,'run_live',forbidden)
    args=['--stage','1','--program',str(BASE),'--source',str(source),
          '--overlay',str(overlay),'--report-root',str(reports)]
    assert staged.main(args)==staged.main(args)==0
    assert calls==[overlay,overlay]
    assert len(list(reports.glob('*/batch_plan.json')))==2
    assert not list(source.iterdir()) and not list(overlay.iterdir())


def test_status_mode_writes_nothing(monkeypatch,tmp_path):
    source=tmp_path/'source';source.mkdir();overlay=tmp_path/'overlay';overlay.mkdir()
    reports=tmp_path/'reports'
    monkeypatch.setattr(engine,'build_plan',lambda *a:plan([],source))
    assert staged.main(['--status','--program',str(BASE),'--source',str(source),
                       '--overlay',str(overlay),'--report-root',str(reports)])==0
    assert not reports.exists()


def test_cap_and_stage_validation(tmp_path):
    for stage,limit in [(0,100),(1,0),(1,151)]:
        with pytest.raises(ValueError):staged.select_batch(plan([],tmp_path),stage,tmp_path,limit)


def test_naive_timestamp_refused():
    with pytest.raises(ValueError):staged.review_reason(window(start='2024-10-01T00:00:00'),[])


@pytest.mark.parametrize('fail',[False,True])
def test_live_progress_wrapper_preserves_call_and_disconnects(monkeypatch,tmp_path,fail):
    import asyncio,types
    calls=[]
    class FakeIB:
        async def connectAsync(self,*a,**kw):calls.append(('connect',kw))
        async def reqHistoricalDataAsync(self,*a,**kw):
            calls.append(('request',kw))
            if fail:raise RuntimeError('fake transport failure')
            return [1]
        def disconnect(self):calls.append(('disconnect',None))
    module=types.ModuleType('ib_async');module.IB=FakeIB
    monkeypatch.setitem(sys.modules,'ib_async',module)
    async def recover(ib,p,overlay,pacer,cap,approve_calendar):
        assert cap==1 and approve_calendar
        return await ib.reqHistoricalDataAsync(types.SimpleNamespace(localSymbol='NQZ4'),
                                               endDateTime='test',durationStr='21600 S')
    fake_engine=types.SimpleNamespace(recover=recover)
    fake_core=types.SimpleNamespace(HistoryPacer=lambda a,b:(a,b))
    args=types.SimpleNamespace(host='127.0.0.1',port=7496,client_id=126)
    operation=staged.run_live(fake_engine,fake_core,{'windows':[window()]},tmp_path,args)
    if fail:
        with pytest.raises(RuntimeError,match='fake transport'):asyncio.run(operation)
    else:
        assert asyncio.run(operation)==[1]
    assert calls[0][1]['readonly'] is True
    assert [x[0] for x in calls]==['connect','request','disconnect']


def auto_args(tmp_path,**kw):
    from types import SimpleNamespace
    d=dict(max_hours=8,max_batches=30,stage=None)
    d.update(kw);return SimpleNamespace(**d)


def test_auto_advances_only_when_current_stage_empty(monkeypatch,tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(staged,'STAGES',{1:'NQZ4',2:'NQH5'})
    stages=[];pacers=[];codes=iter([0,10,0,10])
    def one(args,e,c,s,o,p):
        stages.append(args.stage);pacers.append(p);return next(codes)
    monkeypatch.setattr(staged,'one_batch',one)
    assert staged.run_auto(auto_args(tmp_path),None,SimpleNamespace(HistoryPacer=lambda *a:object()),tmp_path,tmp_path)==0
    assert stages==[1,1,2,2]
    assert all(p is pacers[0] for p in pacers)


@pytest.mark.parametrize('code',[2,124,130])
def test_auto_stops_on_error_time_limit_or_interrupt(monkeypatch,tmp_path,code):
    from types import SimpleNamespace
    calls=[]
    monkeypatch.setattr(staged,'one_batch',lambda *a:(calls.append(a[0].stage) or code))
    assert staged.run_auto(auto_args(tmp_path),None,SimpleNamespace(HistoryPacer=lambda *a:object()),tmp_path,tmp_path)==code
    assert calls==[1]


def test_auto_batch_cap(monkeypatch,tmp_path):
    from types import SimpleNamespace
    calls=[]
    monkeypatch.setattr(staged,'one_batch',lambda *a:(calls.append(a[0].stage) or 0))
    assert staged.run_auto(auto_args(tmp_path,max_batches=2),None,SimpleNamespace(HistoryPacer=lambda *a:object()),tmp_path,tmp_path)==0
    assert calls==[1,1]


def test_auto_refuses_prior_unresolved(monkeypatch,tmp_path):
    from types import SimpleNamespace
    run=tmp_path/'run_old';run.mkdir()
    (run/'results.json').write_text(json.dumps([dict(status='PARTIAL_UNRESOLVED',window=window())]))
    monkeypatch.setattr(staged,'one_batch',lambda *a:pytest.fail('Must not run'))
    assert staged.run_auto(auto_args(tmp_path),None,SimpleNamespace(HistoryPacer=lambda *a:object()),tmp_path,tmp_path)==2


def test_lock_refuses_concurrent_and_releases(tmp_path):
    with staged.execution_lock(tmp_path):
        assert (tmp_path/'execution.lock').exists()
        with pytest.raises(RuntimeError,match='Another execution'):
            with staged.execution_lock(tmp_path):pass
    assert not (tmp_path/'execution.lock').exists()


def test_deadline_prevents_request_after_wait(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    class Base:
        async def wait(self):pass
    ticks=iter([10,12])
    monkeypatch.setattr(staged,'time',SimpleNamespace(monotonic=lambda:next(ticks)))
    async def go():await staged.DeadlinePacer(Base(),11).wait()
    # Avoid mocking time.monotonic until after event-loop construction.
    loop=asyncio.new_event_loop()
    try:
        with pytest.raises(staged.AutoTimeLimit):loop.run_until_complete(go())
    finally:loop.close()


def test_auto_preview_cannot_connect_or_lock(monkeypatch,tmp_path):
    source=tmp_path/'source';source.mkdir();overlay=tmp_path/'overlay';overlay.mkdir()
    reports=tmp_path/'reports'
    monkeypatch.setattr(engine,'build_plan',lambda *a:plan([],source))
    monkeypatch.setattr(staged,'run_auto',lambda *a:pytest.fail('Preview cannot execute'))
    assert staged.main(['--auto','--market','NQ','--program',str(BASE),'--source',str(source),
                       '--overlay',str(overlay),'--report-root',str(reports)])==0
    assert not reports.exists()


def test_es_stage_mapping_and_market_isolation(tmp_path):
    assert staged.stage_symbols('ES')[1]=='ESZ4'
    assert staged.stage_symbols('ES')[7]=='ESU6'
    ws=[window(),window(symbol='ES',localSymbol='ESZ4',conId=495512557)]
    selected,normal,held,_=staged.select_batch(plan(ws,tmp_path),1,tmp_path,100,'ES')
    assert len(selected)==1 and selected[0]['symbol']=='ES'


def test_es_build_uses_es_overlay_and_symbol(tmp_path):
    from types import SimpleNamespace
    calls=[]
    engine=SimpleNamespace(build_plan=lambda *a:(calls.append(a) or {}))
    staged.build_current(engine,tmp_path/'source',tmp_path/'ES_Recovery','ES')
    assert calls==[(tmp_path/'source',('ES',),tmp_path/'ES_Recovery')]


def test_combined_auto_is_serial_and_shares_global_pacer(monkeypatch,tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(staged,'STAGES',{1:'NQZ4'})
    calls=[];codes=iter([0,10,0,10]);pacers=[]
    def one(args,e,c,s,overlay,pacer):
        calls.append((args.market,args.stage,overlay));pacers.append(pacer)
        return next(codes)
    monkeypatch.setattr(staged,'one_batch',one)
    paths={'NQ':tmp_path/'NQ_Recovery','ES':tmp_path/'ES_Recovery'}
    assert staged.run_auto(auto_args(tmp_path,markets=['NQ','ES']),None,
                           SimpleNamespace(HistoryPacer=lambda *a:object()),tmp_path,paths)==0
    assert calls==[('NQ',1,paths['NQ']),('NQ',1,paths['NQ']),('ES',1,paths['ES']),('ES',1,paths['ES'])]
    assert all(p is pacers[0] for p in pacers)


def test_combined_auto_global_batch_cap_not_reset_for_es(monkeypatch,tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(staged,'STAGES',{1:'NQZ4'})
    calls=[];codes=iter([0,10,0])
    monkeypatch.setattr(staged,'one_batch',lambda *a:(calls.append(a[0].market) or next(codes)))
    paths={'NQ':tmp_path/'NQ_Recovery','ES':tmp_path/'ES_Recovery'}
    assert staged.run_auto(auto_args(tmp_path,markets=['NQ','ES'],max_batches=2),None,
                           SimpleNamespace(HistoryPacer=lambda *a:object()),tmp_path,paths)==0
    assert calls==['NQ','NQ','ES']


def test_combined_preview_routes_existing_overlays_without_writes(monkeypatch,tmp_path):
    source=tmp_path/'source';source.mkdir();reports=tmp_path/'reports'
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    calls=[]
    def build(src,symbols,overlay):calls.append((symbols,overlay));return plan([],source)
    monkeypatch.setattr(engine,'build_plan',build)
    assert staged.main(['--auto','--program',str(BASE),
                       '--source',str(source),'--report-root',str(reports)])==0
    assert calls==[(('NQ',),tmp_path/'Documents'/'NQ_Recovery'),(('ES',),tmp_path/'Documents'/'ES_Recovery')]
    assert not reports.exists()


def test_both_rejects_single_overlay_override(tmp_path):
    with pytest.raises(SystemExit):
        staged.main(['--auto','--market','BOTH','--overlay',str(tmp_path)])
