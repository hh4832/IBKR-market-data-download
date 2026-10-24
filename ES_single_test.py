"""Portable reviewed ES tests; preview by default, no private saved plan required."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

import NQ_staged_recovery as staged

DATES={'ESZ4':'2024-12-10','ESM5':'2025-04-08','ESU6':'2026-07-14'}


def select_test_windows(plan,batch=False):
    selected=[]
    for w in plan['windows']:
        start=datetime.fromisoformat(w['start_utc']).astimezone(timezone.utc)
        if batch:
            include=(w['localSymbol'] in DATES and start.date().isoformat()==DATES[w['localSymbol']]
                     and w['missing_minutes']>=180)
        else:
            include=(w['localSymbol']=='ESH5' and start.isoformat()=='2024-12-10T00:00:00+00:00'
                     and w['duration_seconds']==21600)
        if include:
            selected.append(w)
    limit=12 if batch else 1
    if len(selected)>limit:
        raise ValueError('Unexpected reviewed window subdivision; inspect plan before execution')
    return selected


def main(argv=None):
    home=Path.home()/'Documents'
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch',action='store_true')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--data-root',type=Path,default=home/'IBKR_NQ_Snapshot_01')
    parser.add_argument('--recovery-root',type=Path,default=home/'ES_Recovery')
    parser.add_argument('--report-root',type=Path,default=home/'ES_Test_Reports')
    parser.add_argument('--port',type=int,default=7496)
    args=parser.parse_args(argv)
    if args.execute and Path(sys.prefix).name.lower()!='ibkr_dl_311':
        parser.error('Activate ibkr_dl_311 before executing')
    import ibkr_history_recovery as engine
    source,overlay=engine.separate(args.data_root,args.recovery_root)
    engine.separate(source,args.report_root)
    if overlay.exists():engine.separate(overlay,args.report_root)
    def operation():
        current=staged.build_current(engine,source,overlay,'ES')
        current['windows']=select_test_windows(current,args.batch)
        if not current['windows']:
            print('NO_REVIEWED_TEST_WINDOWS_REMAIN. Already observed or no longer in plan; no request issued.')
            return 0
        folder=args.report_root/f'test_{uuid4().hex}'
        folder.mkdir(parents=True,exist_ok=False)
        path=folder/'test_plan.json'
        path.write_text(json.dumps(current,indent=2),encoding='utf-8')
        command=['recover','--plan',str(path),'--recovery-root',str(overlay),
                 '--port',str(args.port),'--client-id','127','--max-requests',str(len(current['windows']))]
        if args.execute:command+=['--approve-calendar','--execute']
        return engine.main(command)
    if args.execute:
        with staged.execution_lock(home/'NQ_Stage_Reports'):
            return operation()
    return operation()


if __name__=='__main__':
    raise SystemExit(main())
