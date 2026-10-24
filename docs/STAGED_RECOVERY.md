# NQ + ES staged and unattended recovery

Windows local only. Keep TWS logged in with read-only socket API; verify the actual
port (default 7496). Never run this collector in GitHub Actions or alongside another
downloader. See [root-cause evidence and the independent recovery model](NQ_RECOVERY.md).

## Setup and preview

Use the independent Conda environment from README. Stop a running older collector
before pulling/updating code or starting this runner. Updating files does not update
an already running Python process.

From the repository root in Anaconda Prompt:

```bat
conda activate ibkr_dl_311
python -B IBKR_staged_recovery.py --auto --market BOTH
```

This is preview only: it rebuilds the plan and prints each stage's normal/held
counts without a TWS connection. Source defaults to the verified snapshot
`%USERPROFILE%\Documents\IBKR_NQ_Snapshot_01`. Use `--source` to explicitly select
a different verified snapshot. No private saved plan JSON is distributed in Git.

## Unattended execution

```bat
python -B IBKR_staged_recovery.py --auto --market BOTH --execute --max-hours 8 --max-batches 30
```

The runner processes NQ then ES, serially. Each symbol's general stages follow:
Z4, H5, M5, U5, Z5, H6, U6. M6 and Z6 are held for targeted review because their
existing datasets were already nearly complete and residuals need calendar/tail review.

Defaults:
- Up to 100 requests per batch (`--max-requests`, range 1..150).
- One shared limiter across batches AND symbols: 12 seconds minimum, 45 per rolling
  ten minutes. Switching stages/markets does not reset the allowance.
- One combined eight-hour / 30-batch budget, not eight hours for each market.
- One execution lock, including manual staged and reviewed ES test execution.
- A fresh source + all-overlay plan before every batch; no rewind/reset of the
  original checkpoint and no silent re-query of completed chunks.

NQ data stays under `%USERPROFILE%\Documents\NQ_Recovery`; ES under
`%USERPROFILE%\Documents\ES_Recovery`. Combined logs and per-batch reports stay
under `%USERPROFILE%\Documents\NQ_Stage_Reports`; report names include the market.
The source snapshot and original Downloads data remain read-only.

Every completed batch must have the expected number of results, all statuses
`OBSERVED_ALL_CANDIDATE_MINUTES`, and a passing source-unchanged check before the
automatic loop continues. Failures or partial replies stop unattended progression
after the current bounded batch. Known prior unresolved runs block automatic startup
pending review. There is no infinite retry/reconnect loop.

## Stopping, resuming and sleep

Press Ctrl+C once and wait for the prompt. Completed chunk files remain available.
Re-run the same auto command after reconnecting: plans are rebuilt from actual saved
bars, not a remembered batch number. A time cap is checked before transport calls;
an in-flight call or planning operation can extend a little beyond the nominal cap.

The Windows process temporarily requests that the system remain awake and releases
the request on exit. This does not change permanent power settings or prevent a
user-forced shutdown, lid-close sleep, TWS auto-logout, network loss or power failure.
Keep the computer plugged in, lid open, and TWS/terminal running.

If `execution.lock` exists, do not delete it while another process is running. A
crash/power loss can leave a stale lock; inspect it before removing/restarting.

## Manual/status commands

```bat
python -B IBKR_staged_recovery.py --status --market BOTH
python -B IBKR_staged_recovery.py --market ES --stage 1
python -B IBKR_staged_recovery.py --market ES --stage 1 --execute
python -B IBKR_staged_recovery.py --auto --market NQ --execute
python -B IBKR_staged_recovery.py --auto --market ES --execute
```

The legacy name `NQ_staged_recovery.py` remains supported. Manual stages default to
NQ; auto defaults to BOTH. Explicit `--market` is recommended. Only single-market
mode accepts a custom `--overlay` override.

## Reviewed ES smoke tests

```bat
python -B ES_single_test.py
python -B ES_single_test.py --execute
python -B ES_single_test.py --batch --execute
```

The single test is ESH5, 2024-12-10 00:00..06:00 UTC (360 candidate minutes).
The diverse test covers ordinary dates in ESZ4/ESM5/ESU6, up to 12 requests.
Unlike the initial local prototype, the published CLI rebuilds its plan from the
snapshot + ES overlay; it does not require private JSON files or re-fetch completed
test windows. Fully recovered tests print `NO_REVIEWED_TEST_WINDOWS_REMAIN`.

Recorded real-TWS smoke results: NQ and ES each passed a single six-hour test and
a multi-contract winter/summer set. ES returned 4,200 observed minutes across 13
requests in that test sequence. These are observed test windows, not certification
of every contract or proof of the exact old `1 D` server/session interpretation.

## What remains for QC

Holiday buffers, expiry boundaries, holes smaller than 180 minutes, unsupported
calendar years and previous unresolved intervals are held. The dated exclusion list
is deliberately conservative and is not an official CME holiday calendar; not every
held day is closed. `AUTO_NORMAL_STAGES_FINISHED_HELD_CASES_REMAIN` means only the
general-stage queue finished. Residual review, source+overlay union, duplicates,
OHLC conflicts, timezone/session checks and independent coverage QC are still needed.
The legacy consolidated Parquet is not automatically rebuilt or served with overlays.
