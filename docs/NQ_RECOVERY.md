# NQ/ES missing windows: guarded requests and independent recovery

## What the original Windows data establishes (2026-10-24)

The v1.5 import at `0d10e58` is byte-identical to the local v1.5 core.
Read-only inspection of all 743 NQ and 743 ES historical chunks shows the same
systematic problem in both symbols. These are minute observations, not adjusted
continuous futures or an official exchange completeness certificate.

|NQ contract|Observed unique rows, all chunks|Coverage inside checkpoint plan, weekly heuristic|60/120-row successful requests|
|---|---:|---:|---:|
|NQZ4|27,255|25.03%|75|
|NQH5|28,385|26.11%|72|
|NQM5|31,138|28.77%|71|
|NQU5|30,956|28.61%|71|
|NQZ5|28,328|26.07%|72|
|NQH6|30,554|28.20%|70|
|NQM6|101,355|99.31%|1|
|NQU6|29,820|27.15%|75|
|NQZ6|42,419|99.44%|0|

These percentages use each saved checkpoint's original bounds and standard weekly
Globex hours, not a verified CME holiday/expiry calendar. They differ from other
QC definitions and from the *latest* saved download plan, whose bounds may extend
beyond the checkpoint. The planner below uses the explicitly saved download plan.
Missing minutes near holidays, excluded final expiry sessions, or newer plan tails
must not automatically be classified as bad historical responses.

Concrete example: the chunk ending 2024-12-11 00:00 UTC contains only 60 bars,
2024-12-10 23:00–23:59 UTC. The prior 22 regular trading hours are absent. A summer
analogue retains 22:00–23:59 UTC (120 bars). The same pattern recurs for ES.
By contrast, NQM6 and NQZ6 have 21:00 UTC checkpoint phases and mostly 1,380-row
regular-session requests. A short Sunday reopening segment can legitimately have
only 60 or 120 bars; row count without a time window is insufficient.

### Confirmed code defect versus transport hypothesis

Confirmed: `request_one_day` previously accepted *any nonempty response after
filtering*, and `backfill_one_contract` advanced by 24 hours. It did not validate
the requested window's coverage. Old `already_complete` states therefore skip
large internal holes. Changing v1.5's terminal endpoint does not repair existing
midnight-phase chunks or rewind their cursor.

Strongly supported but not live-proven: `durationStr="1 D"` combined with a UTC
midnight endpoint is returning/filtering only a new trading-session fragment.
Original logs do not record the *unfiltered* response's first/last timestamps, so
they cannot distinguish a server/session interpretation from client filtering of
an unexpectedly bounded response. Do not claim a specific IBKR server defect or
that throttling/permissions caused this without a controlled raw-response probe.

## Intentional behavior changes after the v1.5 import

`docs/v15_source_manifest.json` remains the historical import record. The core is
now intentionally changed, rather than silently claimed to be the original bytes:

- NQ/ES daily requests use explicit elapsed `86400 S` instead of `1 D`.
- A response with no bars during expected weekly-open minutes, less than half a
  substantial expected window, or a continuous 180-minute regular-hours hole is
  rejected before chunk/log/checkpoint commit. No automatic same-window retry is
  made for a quality rejection; the operator must inspect it. The normal existing
  transport-error retry policy and pacing are unchanged.
- A legitimate holiday/early-close/thin session may trigger conservative review.
  Acceptance is **not** full coverage certification. No bars are fabricated.
- New request logs record explicit duration, first/last retained timestamps and
  the heuristic result. Exhausted cursors include `coverage_verified=False`.
- Stocks retain the v1.5 request semantics; this change does not certify their
  history or migrate any existing checkpoint.

Explicit seconds plus the guard prevents the demonstrated false-success path,
but live protocol behavior must be verified before a large backfill. This change
does not retroactively repair already written files.

## Safe sequence after merging/pulling this change

1. Stop existing downloaders. Keep the original dataset read-only. Use the
   existing `copy_existing_history.py` preview/`--execute` workflow to create and
   SHA256-verify a separate snapshot before any live repair. No reset/deletion of
   `checkpoint.json`, chunks or consolidated Parquet is needed.
2. Activate the independent `ibkr_dl_311` environment, using the README's bounded
   dependencies. Do not upgrade/downgrade the existing research `py311` in place.
3. Generate an offline plan from the snapshot. Program and report paths are
   separate from the dataset; use short output paths on Windows.

```bat
conda activate ibkr_dl_311
cd /d "%USERPROFILE%\Documents\IBKR-market-data-download"
python -B ibkr_history_recovery.py plan --data-root "%USERPROFILE%\Documents\IBKR_Data_Sandbox\IBKR_MarketData" --report-dir "%USERPROFILE%\Documents\NQ_Repair_Plan_01" --symbols NQ
python -B ibkr_history_recovery.py recover --plan "%USERPROFILE%\Documents\NQ_Repair_Plan_01\recovery_plan.json" --recovery-root "%USERPROFILE%\Documents\NQ_Recovery"
```

The second command is **preview only**: no connection, request, or recovery-file
write. Report directories must be new. Planning reads `download_plan.csv`, hashes
the selected original contract files and generates explicit UTC missing spans,
at most six hours each. It does not use `next_cursor_utc` as proof of coverage.
The plan is a private local artifact containing source paths/hashes; never commit it.

4. Review `candidate_windows.csv` against CME holidays/halts/expiry exclusions and
   select a known ordinary weekday with a demonstrated multi-hour hole. The JSON
   plan can be reduced to the reviewed window(s); keep its original source hashes.
   Capture a single-date live comparison before approving bulk recovery:
   the old `1 D`/midnight query, `86400 S` query, and explicit intraday seconds
   query. Record raw/retained first/last timestamps, counts, conId, TRADES,
   useRTH=False, duration, endpoint, and HMDS events. This live probe has not been
   executed by the offline patch or CI.
5. Only after those checks, the explicitly enabled recovery command is:

```bat
python -B ibkr_history_recovery.py recover --plan "%USERPROFILE%\Documents\NQ_Repair_Plan_01\recovery_plan.json" --recovery-root "%USERPROFILE%\Documents\NQ_Recovery" --port 7496 --client-id 126 --max-requests 1 --approve-calendar --execute
```

This command **does** connect to TWS and download. Verify the actual TWS port,
read-only API setting and unused client ID first. The tool does not modify those
settings. It sends one request per missing subwindow, paces every call at the
existing 12 seconds / 45 per ten minutes policy, caps *actual* calls, and performs
no hidden retries. IncludeExpired is set for actual quarterly conIds.

New bars go under a unique `NQ_Recovery/run_<uuid>/raw/.../chunks/` directory.
Original chunks, logs, checkpoint, consolidated files and OHLC remain untouched.
Original timestamps are excluded before saving new bars. Partial replies remain
`PARTIAL_UNRESOLVED`; even all candidate minutes observed is not an official
calendar certificate. Raw first/last response timestamps, errors, plan and saved
hashes are retained in the new run directory. Changes to hashed source files
invalidate the plan and stop recovery.

6. Re-plan with `--overlay-run "%USERPROFILE%\Documents\NQ_Recovery"` to subtract
   all completed runs. Use a *new* report directory each time. Rerunning the old
   plan deliberately does not silently mutate it; without re-planning it would
   request the first windows again. Preserve the new logs and run independent QC.

For analysis, `read_overlay(recovery_root, symbol, contract_folder_name)` reads
all recovery runs; concatenate it with `read_source(original_contract_folder)`,
put original rows last and deduplicate `(conId, timestamp_utc)` keeping last.
`load_with_recovery` provides this original-wins union for a single run's contract
folder. The legacy loader reads only original chunks and does not auto-merge or
serve recovery bars. No original file replacement is part of this change.

## Validation boundary

Offline tests reproduce winter 60-bar and summer 120-bar fragments, allow the
legitimate Sunday boundary, reject an internal gap or holiday-sized short session
for review, retain old checkpoint bytes, plan holes behind an exhausted cursor,
enforce source hashes/preview/request cap, and keep partial recovery unresolved.
Real Parquet fixtures exercise recovery rather than merely asserting filenames.
Network is blocked in test fixtures. Live API behavior and full post-repair
coverage remain pending; the change should stay draft until reviewed/probed.

References: [IBKR request parameters](https://interactivebrokers.github.io/tws-api/historical_bars.html)
and [historical step sizes / retention](https://interactivebrokers.github.io/tws-api/historical_limitations.html).
Expired futures older than two years after expiry may no longer be recoverable;
do not postpone the earliest contracts indefinitely or assume source availability.
