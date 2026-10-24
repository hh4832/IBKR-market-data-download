"""Offline missing-window plan + explicit, bounded, append-only TWS recovery overlay.

The original dataset and its checkpoints are always read-only. Candidate missing
minutes use a weekly-hours heuristic and MUST be reviewed against holidays/halts.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import stat
import re
from pathlib import Path
from uuid import uuid4

import pandas as pd

from ibkr_window_quality import regular_minutes


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def separate(source: Path, dest: Path) -> tuple[Path, Path]:
    source = source.resolve(strict=True)
    dest = dest.absolute()
    for p in [dest, *dest.parents]:
        if p.exists():
            info=p.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                raise ValueError('Recovery path must not traverse junctions/symlinks')
    dest=dest.resolve()
    if source == dest or source in dest.parents or dest in source.parents:
        raise ValueError('Recovery/report destination must be separate from source data')
    return source, dest


def read_source(folder: Path) -> pd.DataFrame:
    paths = sorted((folder/'chunks').glob('*.parquet'))
    if not paths:
        return pd.DataFrame(columns=['timestamp_utc', 'conId'])
    return pd.concat((pd.read_parquet(p) for p in paths), ignore_index=True).drop_duplicates(
        ['conId', 'timestamp_utc'], keep='last')


def read_overlay(root: Path, symbol: str, folder: str) -> pd.DataFrame:
    runs=[root] if (root/'raw').is_dir() else sorted(root.glob('run_*'))
    frames=[read_source(run/'raw'/symbol/folder) for run in runs]
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=['timestamp_utc','conId'])


def load_with_recovery(source_folder: Path, overlay_folder: Path) -> pd.DataFrame:
    """Union real bars only; original wins on overlaps, never fabricate OHLC."""
    original, extra = read_source(source_folder), read_source(overlay_folder)
    return pd.concat([extra, original], ignore_index=True).drop_duplicates(
        ['conId', 'timestamp_utc'], keep='last').sort_values('timestamp_utc').reset_index(drop=True)


def _spans(missing: pd.DatetimeIndex, max_minutes=360):
    start = previous = None
    for t in missing:
        if start is not None and (t-previous != pd.Timedelta(minutes=1)
                                  or t-start >= pd.Timedelta(minutes=max_minutes)):
            yield start, previous+pd.Timedelta(minutes=1)
            start = None
        if start is None:
            start = t
        previous = t
    if start is not None:
        yield start, previous+pd.Timedelta(minutes=1)


def build_plan(data_root: Path, symbols=('NQ',), overlay: Path | None = None) -> dict:
    """Use explicit saved download_plan.csv bounds, not an exhausted checkpoint."""
    data_root = data_root.resolve(strict=True)
    if not set(symbols).issubset({'NQ', 'ES'}) or not symbols:
        raise ValueError('Recovery supports explicit NQ/ES only')
    if overlay is not None:
        _, overlay = separate(data_root, overlay)
    table = data_root/'download_plan.csv'
    rows = pd.read_csv(table, keep_default_na=False)
    windows, sources, summary = [], {str(table): digest(table)}, []
    selected = rows[rows.symbol.isin(symbols)]
    if selected.empty or selected.duplicated(['symbol', 'conId']).any():
        raise ValueError('Saved plan has no selected contracts or duplicate identities')
    for row in selected.to_dict('records'):
        symbol, local, conid = row['symbol'], row['localSymbol'], int(row['conId'])
        if any('/' in s or '\\' in s or s in ('.','..') for s in (symbol, local)):
            raise ValueError('Unsafe contract path')
        folder = data_root/'raw'/symbol/f'{local}_{conid}'
        if not folder.is_dir():
            raise FileNotFoundError(folder)
        before = {str(p): digest(p) for p in folder.rglob('*') if p.is_file()}
        frame = read_source(folder)
        if overlay:
            frame=pd.concat([read_overlay(overlay,symbol,folder.name),frame],ignore_index=True).drop_duplicates(
                ['conId','timestamp_utc'],keep='last')
        if len(frame) and set(frame.conId.astype(int)) != {conid}:
            raise ValueError('conId mismatch in original/overlay data')
        expected = regular_minutes(row['start_utc'], row['end_utc'])
        observed = pd.DatetimeIndex(pd.to_datetime(frame.timestamp_utc, utc=True))
        missing = expected[~expected.isin(observed)]
        new = []
        for start, end in _spans(missing):
            identity = f'{symbol}|{conid}|{start.isoformat()}|{end.isoformat()}'
            new.append(dict(id=hashlib.sha256(identity.encode()).hexdigest()[:24],
                            symbol=symbol, localSymbol=local, conId=conid,
                            expiry=str(row.get('expiry','')),
                            start_utc=start.isoformat(), end_utc=end.isoformat(),
                            duration_seconds=int((end-start).total_seconds()),
                            missing_minutes=int((end-start).total_seconds()/60),
                            status='CANDIDATE_REQUIRES_HOLIDAY_HALT_AND_EXPIRY_REVIEW'))
        # Detect a concurrent original downloader changing data during planning.
        after = {str(p): digest(p) for p in folder.rglob('*') if p.is_file()}
        if before != after:
            raise RuntimeError('Original dataset changed during planning; stop all downloaders first')
        sources.update(before)
        windows.extend(new)
        summary.append(dict(symbol=symbol, localSymbol=local, conId=conid,
                            expected_standard_minutes=len(expected),
                            observed_standard_minutes=len(expected)-len(missing),
                            missing_candidate_minutes=len(missing), windows=len(new)))
    return dict(schema_version=1, source_root=str(data_root), source_hashes=sources,
                prior_overlay=str(overlay) if overlay else None,
                calendar='WEEKLY_HEURISTIC_NOT_HOLIDAY_VERIFIED',
                summary=summary, windows=windows)


def verify_source(plan: dict) -> None:
    root = Path(plan['source_root']).resolve(strict=True)
    for path, checksum in plan['source_hashes'].items():
        p = Path(path).resolve(strict=True)
        if root not in p.parents or digest(p) != checksum:
            raise RuntimeError(f'Source changed or plan references path outside dataset: {p}')


def _validate_windows(plan: dict) -> None:
    identities = {(r['symbol'], r['localSymbol'], int(r['conId'])) for r in plan['summary']}
    for w in plan['windows']:
        start, end = pd.Timestamp(w['start_utc']), pd.Timestamp(w['end_utc'])
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError('Recovery windows must be UTC-aware')
        seconds = int((end-start).total_seconds())
        if seconds != w['duration_seconds'] or not 60 <= seconds <= 21600 or seconds % 60:
            raise ValueError('Recovery duration outside validated 1 minute..6 hour bounds')
        if (w['symbol'], w['localSymbol'], int(w['conId'])) not in identities:
            raise ValueError('Recovery window contract not in plan summary')
        if (w['symbol'] not in {'NQ', 'ES'} or any(c in w['localSymbol'] for c in '/\\')
                or w['localSymbol'] in ('.','..') or not re.fullmatch(r'[0-9a-f]{24}',w['id'])):
            raise ValueError('Unsafe recovery contract')


async def recover(ib, plan: dict, overlay: Path, pacer, max_requests: int,
                  *, approve_calendar: bool = False) -> dict:
    """Explicit live operation; cannot write anywhere under the original root.

    Every transport call is paced and counted, no automatic retries. New real bars
    go to a unique run directory. Unfilled minutes remain unresolved for re-plan.
    """
    if not approve_calendar or max_requests < 1:
        raise ValueError('Calendar review approval and positive request cap are required')
    source, overlay = separate(Path(plan['source_root']), overlay)
    _validate_windows(plan)
    verify_source(plan)
    if not ib.isConnected():
        raise RuntimeError('TWS not connected')
    from ib_async import Contract
    import ibkr_history_core as core
    run = overlay/f'run_{uuid4().hex}'
    run.mkdir(parents=True, exist_ok=False)
    (run/'plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    results=[]
    for w in plan['windows'][:max_requests]:
        start, end = pd.Timestamp(w['start_utc']), pd.Timestamp(w['end_utc'])
        c = Contract(secType='FUT', symbol=w['symbol'], localSymbol=w['localSymbol'],
                     conId=w['conId'], exchange='CME', currency='USD', includeExpired=True,
                     lastTradeDateOrContractMonth=w.get('expiry','').replace('-',''))
        await pacer.wait()
        events=[]
        def record(req_id, code, message, event_contract):
            events.append(dict(req_id=req_id,code=code,message=str(message)))
        ib.errorEvent += record
        try:
            bars=await ib.reqHistoricalDataAsync(c,endDateTime=end.to_pydatetime(),
                durationStr=f"{w['duration_seconds']} S",barSizeSetting='1 min',
                whatToShow='TRADES',useRTH=False,formatDate=2,keepUpToDate=False,timeout=75)
            frame=core.bars_to_frame(bars,c)
        except Exception as exc:
            results.append(dict(window=w,status='FAILED_UNRESOLVED',error=str(exc),errors=events))
            break
        finally:
            ib.errorEvent -= record
        raw_first=frame.timestamp_utc.min().isoformat() if len(frame) else None
        raw_last=frame.timestamp_utc.max().isoformat() if len(frame) else None
        if any(core._fatal_ibkr_message(x['code'],x['message']) for x in events):
            results.append(dict(window=w,status='API_ERROR_UNRESOLVED',errors=events,
                                returned_first=raw_first,returned_last=raw_last))
            break
        frame=frame[(frame.timestamp_utc>=start)&(frame.timestamp_utc<end)].copy()
        original=read_source(source/'raw'/w['symbol']/f"{w['localSymbol']}_{w['conId']}")
        original_ts=pd.DatetimeIndex(pd.to_datetime(original.timestamp_utc,utc=True))
        frame=frame[~frame.timestamp_utc.isin(original_ts)].copy()
        quality=core.validate_frame(frame)
        if quality['null_ohlc'] or quality['invalid_ohlc'] or quality['unaligned_minute']:
            results.append(dict(window=w,status='QUALITY_ERROR_UNRESOLVED',quality=quality))
            break
        dest=run/'raw'/w['symbol']/f"{w['localSymbol']}_{w['conId']}"/'chunks'/f"recovery_{w['id']}.parquet"
        if len(frame):
            core.write_chunk(dest,frame)
        expected=regular_minutes(start,end)
        remaining=int((~expected.isin(pd.DatetimeIndex(frame.timestamp_utc))).sum())
        results.append(dict(window=w,status=('OBSERVED_ALL_CANDIDATE_MINUTES' if remaining==0
                                            else 'PARTIAL_UNRESOLVED'),rows=len(frame),
                            unresolved_minutes=remaining,returned_first=raw_first,
                            returned_last=raw_last,errors=events,
                            saved_sha256=digest(dest) if len(frame) else None))
        # Persist after each call, so an interruption retains an honest partial record.
        (run/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    (run/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    verify_source(plan)
    return dict(run_dir=str(run),requests=len(results),results=results,
                source_checkpoints_unchanged=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('plan');p.add_argument('--data-root',type=Path,required=True)
    p.add_argument('--report-dir',type=Path,required=True);p.add_argument('--symbols',nargs='+',default=['NQ'])
    p.add_argument('--overlay-run',type=Path)
    r=sub.add_parser('recover');r.add_argument('--plan',type=Path,required=True)
    r.add_argument('--recovery-root',type=Path,required=True)
    r.add_argument('--execute',action='store_true');r.add_argument('--approve-calendar',action='store_true')
    r.add_argument('--max-requests',type=int,default=1);r.add_argument('--host',default='127.0.0.1')
    r.add_argument('--port',type=int,default=7496);r.add_argument('--client-id',type=int,default=126)
    args=parser.parse_args(argv)
    if args.command=='plan':
        source,dest=separate(args.data_root,args.report_dir)
        if dest.exists():parser.error('Use a new report directory; existing plans are not overwritten')
        plan=build_plan(source,tuple(args.symbols),args.overlay_run)
        dest.mkdir(parents=True,exist_ok=False)
        (dest/'recovery_plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
        pd.DataFrame(plan['windows']).to_csv(dest/'candidate_windows.csv',index=False)
        print(json.dumps(plan['summary'],indent=2));return 0
    plan=json.loads(args.plan.read_text(encoding='utf-8'))
    separate(Path(plan['source_root']),args.recovery_root)
    _validate_windows(plan);verify_source(plan)
    if not args.execute:
        print(f"PREVIEW ONLY: {len(plan['windows'])} candidates; no TWS connection or writes")
        return 0
    if not args.approve_calendar:parser.error('Review holidays/halts/expiry; then pass --approve-calendar')
    if args.max_requests<1:parser.error('--max-requests must be positive')
    async def live():
        from ib_async import IB
        from ibkr_history_core import HistoryPacer
        ib=IB()
        try:
            await ib.connectAsync(args.host,args.port,clientId=args.client_id,readonly=True,timeout=15)
            return await recover(ib,plan,args.recovery_root,HistoryPacer(12,45),args.max_requests,
                                 approve_calendar=True)
        finally:
            ib.disconnect()
    result=asyncio.run(live());print(json.dumps(result,indent=2));return 0


if __name__=='__main__':
    raise SystemExit(main())
