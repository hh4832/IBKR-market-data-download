"""Bounded NQ/ES recovery stages. Preview by default; --execute enables recovery.

Each invocation rebuilds the missing plan from source + ALL saved overlay runs.
Conservative holiday buffers, tiny holes and previously unresolved intervals are
held for review. This is not an exchange-certified holiday/completeness calendar.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
from uuid import uuid4
import warnings

import pandas as pd

STAGES = {1:'NQZ4', 2:'NQH5', 3:'NQM5', 4:'NQU5', 5:'NQZ5', 6:'NQH6', 7:'NQU6'}


def stage_symbols(market='NQ'):
    if market not in {'NQ','ES'}:
        raise ValueError('A stage must select one market: NQ or ES')
    return {stage:symbol.replace('NQ',market,1) for stage,symbol in STAGES.items()}
# Deliberately over-exclude bank/equity holidays and surrounding days. These are
# review dates, NOT assertions of CME closures. CME product schedules still govern.
REVIEW_DATES = {
    2024: ('01-01','01-15','02-19','03-29','05-27','06-19','07-04','09-02','10-14','11-11','11-28','12-25'),
    2025: ('01-01','01-09','01-20','02-17','04-18','05-26','06-19','07-04','09-01','10-13','11-11','11-27','12-25'),
    2026: ('01-01','01-19','02-16','04-03','05-25','06-19','07-03','07-04','09-07','10-12','11-11','11-26','12-25'),
}
HOLIDAYS = {date.fromisoformat(f'{year}-{s}') for year, values in REVIEW_DATES.items() for s in values}


def stamp(value):
    t = pd.Timestamp(value)
    if t.tzinfo is None:
        raise ValueError('Timezone-aware window required')
    return t.tz_convert('UTC')


def previous_unresolved(overlay: Path):
    intervals, warnings_found = [], []
    for run in sorted(overlay.glob('run_*')):
        result_file = run/'results.json'
        if not result_file.exists():
            # A first-call interruption may legitimately leave only plan.json.
            continue
        try:
            entries = json.loads(result_file.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise RuntimeError(f'Cannot safely read prior results: {result_file}') from exc
        for result in entries:
            if result.get('status') != 'OBSERVED_ALL_CANDIDATE_MINUTES':
                w = result['window']
                intervals.append((int(w['conId']), stamp(w['start_utc']), stamp(w['end_utc'])))
                warnings_found.append(dict(run=str(run), status=result.get('status'), window=w))
    return intervals, warnings_found


def review_reason(w, blocked):
    start, end = stamp(w['start_utc']), stamp(w['end_utc'])
    if int(w['missing_minutes']) < 180:
        return 'SMALL_GAP_REVIEW'
    if any(int(w['conId']) == conid and start < b and end > a for conid, a, b in blocked):
        return 'PREVIOUS_UNRESOLVED_REVIEW'
    local_start = start.tz_convert('America/Chicago').date()
    local_end = (end-pd.Timedelta(microseconds=1)).tz_convert('America/Chicago').date()
    day = local_start
    while day <= local_end:
        if day.year not in REVIEW_DATES:
            return 'CALENDAR_YEAR_NOT_REVIEWED'
        if any(day+timedelta(days=delta) in HOLIDAYS for delta in range(-2,3)):
            return 'HOLIDAY_BUFFER_REVIEW'
        day += timedelta(days=1)
    expiry = date.fromisoformat(w['expiry'])
    if local_start <= expiry+timedelta(days=1) and local_end >= expiry-timedelta(days=2):
        return 'EXPIRY_BOUNDARY_REVIEW'
    return None


def select_batch(plan, stage, overlay, limit, market='NQ'):
    stages=stage_symbols(market)
    if stage not in stages or not 1 <= limit <= 150:
        raise ValueError('Stage must be 1..7; request limit must be 1..150')
    blocked, previous = previous_unresolved(overlay)
    normal, held = [], []
    for w in plan['windows']:
        if w['symbol']!=market or w['localSymbol'] != stages[stage]:
            continue
        reason = review_reason(w, blocked)
        if reason:
            held.append({**w, 'hold_reason':reason})
        else:
            normal.append(w)
    normal.sort(key=lambda w:(stamp(w['start_utc']),w['id']))
    return normal[:limit], normal, held, previous


def build_current(engine, source, overlay, market='NQ'):
    with warnings.catch_warnings():
        # Known pandas 2.3 advisory from the upstream empty-overlay concat.
        warnings.filterwarnings('ignore', message='The behavior of DataFrame concatenation with empty or all-NA entries.*', category=FutureWarning)
        return engine.build_plan(source, (market,), overlay)


def print_status(plan):
    print('Contract | observed minutes | candidate missing | candidate windows', flush=True)
    for r in plan['summary']:
        print(f"{r['localSymbol']:8} | {r['observed_standard_minutes']:8} | {r['missing_candidate_minutes']:8} | {r['windows']:5}",flush=True)
    print('Candidate minutes include calendar/expiry/availability cases; not all are confirmed gaps.',flush=True)


class AutoTimeLimit(RuntimeError):
    pass


class DeadlinePacer:
    def __init__(self, base, deadline):
        self.base, self.deadline = base, deadline

    async def wait(self):
        if time.monotonic() >= self.deadline:
            raise AutoTimeLimit('Automatic run time limit reached; saved results retained')
        await self.base.wait()
        if time.monotonic() >= self.deadline:
            raise AutoTimeLimit('Automatic run time limit reached; saved results retained')


@contextmanager
def execution_lock(report_root):
    report_root.mkdir(parents=True,exist_ok=True)
    path=report_root/'execution.lock'
    token=uuid4().hex
    try:
        handle=path.open('x',encoding='utf-8')
    except FileExistsError as exc:
        raise RuntimeError(f'Another execution or stale lock exists: {path}. Do not delete it while a downloader is running; request inspection.') from exc
    with handle:
        json.dump(dict(pid=os.getpid(),token=token,started_utc=datetime.now(timezone.utc).isoformat()),handle)
    try:
        yield
    finally:
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('token')==token:
                path.unlink()
        except (OSError,ValueError):
            pass


@contextmanager
def keep_system_awake():
    active=False
    if os.name=='nt':
        import ctypes
        active=bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
        print('Temporary sleep prevention active.' if active else 'Sleep prevention unavailable; keep Windows awake manually.',flush=True)
    try:
        yield
    finally:
        if active:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


class Tee:
    def __init__(self, console, logfile):
        self.console,self.logfile=console,logfile
    def write(self,text):
        self.console.write(text);self.logfile.write(text)
        self.flush()
        return len(text)
    def flush(self):
        self.console.flush();self.logfile.flush()


async def run_live(engine, core, plan, overlay, args, pacer=None):
    from ib_async import IB
    ib = IB()
    try:
        await ib.connectAsync(args.host, args.port, clientId=args.client_id, readonly=True, timeout=15)
        original_request = ib.reqHistoricalDataAsync
        count = 0
        async def progress(*a, **kw):
            nonlocal count
            count += 1
            c = a[0]
            print(f"[{count}/{len(plan['windows'])}] {c.localSymbol} end={kw['endDateTime']} duration={kw['durationStr']}", flush=True)
            bars = await original_request(*a, **kw)
            print(f'    received {len(bars or [])} raw bars; validating and saving',flush=True)
            return bars
        ib.reqHistoricalDataAsync = progress
        return await engine.recover(ib,plan,overlay,pacer or core.HistoryPacer(12,45),len(plan['windows']),approve_calendar=True)
    finally:
        ib.disconnect()


def main(argv=None):
    home = Path.home()/'Documents'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',type=int,choices=range(1,8))
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--auto',action='store_true',help='Run stages 1..7 sequentially, with bounded batches')
    parser.add_argument('--market',choices=['NQ','ES','BOTH'],help='Auto defaults to BOTH (NQ then ES); manual stage defaults to NQ')
    parser.add_argument('--max-hours',type=float,default=8)
    parser.add_argument('--max-batches',type=int,default=30)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--max-requests',type=int,default=100)
    local_program=Path(__file__).resolve().parent
    default_program=local_program if (local_program/'ibkr_history_recovery.py').is_file() else home/'IBKR_NQ_Repair'
    parser.add_argument('--program',type=Path,default=default_program)
    parser.add_argument('--source',type=Path,default=home/'IBKR_NQ_Snapshot_01')
    parser.add_argument('--overlay',type=Path,help='Optional override for a single market only')
    parser.add_argument('--report-root',type=Path,default=home/'NQ_Stage_Reports')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=7496)
    parser.add_argument('--client-id',type=int,default=126)
    args = parser.parse_args(argv)
    if not args.status and args.stage is None and not args.auto:
        parser.error('Choose --stage 1..7, --status or --auto')
    if args.auto and (args.stage is not None or args.status):
        parser.error('--auto cannot be combined with --stage or --status')
    args.market=args.market or ('BOTH' if args.auto else 'NQ')
    if args.market=='BOTH' and args.stage is not None:
        parser.error('Manual --stage needs --market NQ or ES')
    if args.market=='BOTH' and args.overlay is not None:
        parser.error('BOTH uses separate NQ_Recovery and ES_Recovery; --overlay is single-market only')
    args.markets=['NQ','ES'] if args.market=='BOTH' else [args.market]
    if not 0 < args.max_hours <= 24 or not 1 <= args.max_batches <= 100:
        parser.error('Use --max-hours >0 and <=24, --max-batches 1..100')
    if args.status and args.execute:
        parser.error('--status does not download; do not combine with --execute')
    if not 1 <= args.max_requests <= 150:
        parser.error('--max-requests must be 1..150')
    if args.execute and Path(sys.prefix).name.lower() != 'ibkr_dl_311':
        parser.error('Activate conda environment ibkr_dl_311 before live execution')
    sys.path.insert(0,str(args.program.resolve(strict=True)))
    engine=importlib.import_module('ibkr_history_recovery')
    core=importlib.import_module('ibkr_history_core')
    if Path(engine.__file__).resolve().parent != args.program.resolve():
        raise RuntimeError('Imported recovery module is not in selected program folder')
    source=args.source.resolve(strict=True)
    overlays={market:engine.separate(source,args.overlay or home/f'{market}_Recovery')[1]
              for market in args.markets}
    engine.separate(source,args.report_root)
    for overlay in overlays.values():
        if overlay.exists():
            engine.separate(overlay,args.report_root)
    if args.auto:
        if not args.execute:
            print('AUTO PREVIEW: recalculating all stages; no TWS connection.',flush=True)
            for market in args.markets:
                overlay=overlays[market]
                plan=build_current(engine,source,overlay,market)
                for stage,symbol in stage_symbols(market).items():
                    batch,normal,held,_=select_batch(plan,stage,overlay,args.max_requests,market)
                    print(f'{market} stage {stage} {symbol}: normal={len(normal)}, held={len(held)}, next batch={len(batch)}',flush=True)
            print('PREVIEW ONLY. --auto --execute starts bounded live recovery.',flush=True)
            return 0
        with execution_lock(args.report_root),keep_system_awake():
            logfile=args.report_root/f'auto_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}.log'
            print(f'Automatic log: {logfile}',flush=True)
            with logfile.open('x',encoding='utf-8') as handle,redirect_stdout(Tee(sys.stdout,handle)),redirect_stderr(Tee(sys.stderr,handle)):
                try:
                    return run_auto(args,engine,core,source,overlays)
                except Exception:
                    traceback.print_exc()
                    print('AUTO_STOPPED_ERROR. No automatic retry; saved chunks retained. Request log inspection.',flush=True)
                    return 2
    if args.status:
        for market in args.markets:
            print(f'Market {market}:',flush=True)
            print_status(build_current(engine,source,overlays[market],market))
        return 0
    overlay=overlays[args.market]
    if args.execute:
        with execution_lock(args.report_root):
            code=one_batch(args,engine,core,source,overlay)
            return 0 if code==10 else code
    code=one_batch(args,engine,core,source,overlay)
    return 0 if code==10 else code


def run_auto(args,engine,core,source,overlay):
    overlays=overlay if isinstance(overlay,dict) else {'NQ':overlay}
    markets=getattr(args,'markets',list(overlays))
    for market in markets:
        blocked,previous=previous_unresolved(overlays[market])
        if previous:
            print(f'AUTO_STOPPED_REVIEW: {market} previous unresolved results exist. Request inspection before unattended recovery.',flush=True)
            return 2
    deadline=time.monotonic()+args.max_hours*3600
    # Reuse the SAME limiter across all batches and stages: do not reset the
    # rolling 10-minute request allowance at a subprocess/batch boundary.
    pacer=DeadlinePacer(core.HistoryPacer(12,45),deadline)
    batches=0
    for market in markets:
        args.market=market
        print(f'AUTO_MARKET_START: {market}; output={overlays[market]}',flush=True)
        for stage in stage_symbols(market):
            args.stage=stage
            while True:
                if batches>=args.max_batches or time.monotonic()>=deadline:
                    print('AUTO_LIMIT_REACHED. Saved data retained; re-run --auto to recalculate and continue.',flush=True)
                    return 0
                code=one_batch(args,engine,core,source,overlays[market],pacer)
                if code==10:
                    break
                if code!=0:
                    print(f'AUTO_STOPPED_REVIEW_OR_INTERRUPTION: market={market}, stage={stage}, code={code}. No automatic retry.',flush=True)
                    return code
                batches+=1
                print(f'AUTO_BATCH_FINISHED: {batches}/{args.max_batches}; continuing after replanning.',flush=True)
        print(f'AUTO_MARKET_NORMAL_STAGES_FINISHED: {market}; held cases remain.',flush=True)
    print('AUTO_NORMAL_STAGES_FINISHED_HELD_CASES_REMAIN. Request final calendar/expiry review and QC.',flush=True)
    return 0


def one_batch(args,engine,core,source,overlay,pacer=None):
    market=getattr(args,'market','NQ')
    stages=stage_symbols(market)
    print('Rebuilding plan from source + all saved recovery runs. No source files are written.',flush=True)
    plan=build_current(engine,source,overlay,market)
    print_status(plan)
    if args.status:
        return 0
    batch, normal, held, previous=select_batch(plan,args.stage,overlay,args.max_requests,market)
    print(f"{market} stage {args.stage}: {stages[args.stage]}; normal windows remaining={len(normal)}; held for review={len(held)}",flush=True)
    if not batch:
        print('STAGE_NORMAL_WINDOWS_FINISHED. Held cases remain unresolved; proceed to next stage or request review.',flush=True)
        return 10
    plan['windows']=batch
    engine._validate_windows(plan)
    report=args.report_root.resolve()/f"{market}_stage{args.stage:02}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"
    report.mkdir(parents=True,exist_ok=False)
    (report/'batch_plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    (report/'held_windows.json').write_text(json.dumps(held,indent=2),encoding='utf-8')
    info=dict(market=market,stage=args.stage,contract=stages[args.stage],requests_selected=len(batch),
              candidate_minutes_selected=sum(w['missing_minutes'] for w in batch),
              first_start_utc=batch[0]['start_utc'],last_end_utc=batch[-1]['end_utc'],
              normal_windows_before=len(normal),held_windows=len(held),
              hold_reasons=dict(Counter(w['hold_reason'] for w in held)),report_dir=str(report))
    (report/'summary.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
    print(json.dumps(info,indent=2),flush=True)
    if not args.execute:
        print('PREVIEW ONLY. Add --execute for ONE bounded batch. No TWS connection.',flush=True)
        return 0
    before={p for p in overlay.glob('run_*')}
    try:
        if pacer is None:
            result=asyncio.run(run_live(engine,core,plan,overlay,args))
        else:
            result=asyncio.run(run_live(engine,core,plan,overlay,args,pacer=pacer))
    except AutoTimeLimit as exc:
        new_runs=sorted(str(p) for p in overlay.glob('run_*') if p not in before)
        print(f'AUTO_TIME_LIMIT: {exc}',flush=True)
        print(json.dumps({'possible_run_dirs':new_runs,'report_dir':str(report)},indent=2),flush=True)
        return 124
    except KeyboardInterrupt:
        new_runs=sorted(str(p) for p in overlay.glob('run_*') if p not in before)
        print('\nSTOPPED. Saved chunks remain. Re-run the SAME stage command to rebuild the remaining plan.',flush=True)
        print(json.dumps({'possible_run_dirs':new_runs,'report_dir':str(report)},indent=2),flush=True)
        return 130
    counts=dict(Counter(x['status'] for x in result['results']))
    summary=dict(market=market,run_dir=result['run_dir'],requests=result['requests'],statuses=counts,
                 saved_rows=sum(x.get('rows',0) for x in result['results']),
                 unresolved_minutes=sum(x.get('unresolved_minutes',0) for x in result['results']),
                 source_checkpoints_unchanged=result['source_checkpoints_unchanged'],report_dir=str(report))
    (report/'completed_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2),flush=True)
    if (result['requests']!=len(batch) or len(result['results'])!=len(batch)
            or not result.get('source_checkpoints_unchanged')
            or any(s != 'OBSERVED_ALL_CANDIDATE_MINUTES' for s in counts)):
        print('REVIEW REQUIRED. Send run_dir for inspection before another batch.',flush=True)
        return 2
    print('BATCH_FINISHED. Repeat the SAME stage command to recalculate and continue, or stop and move locations.',flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
