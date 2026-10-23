# AGENTS.md — IBKR Market Data Download

## Execution boundaries
- This is the **Windows-local** IBKR TWS / Gateway downloader, not a Colab-based collector. The socket 127.0.0.1 belongs to the *Windows machine with TWS running*. GitHub Actions only runs fake/offline tests.
- Never include order placement, trades, production brokerage actions, or secrets.
- The three files ibkr_history_core.py, ibkr_backfill_watchdog.py and IBKR_NQ_ES_1min_Backfill.ipynb are **recovered original source**, deliberately left unchanged during initial repository import. Document specific future behavior changes separately with tests.

## Data invariants and prior research quality problems
- Original directory layout: raw/<symbol>/<localSymbol>_<conId>/chunks/*.parquet; original checkpoint.json and requests.jsonl must remain backward-compatible.
- source timestamp_utc is timezone-aware UTC, futures contracts are individual conIds (not CONTFUT); whatToShow=TRADES and useRTH=False; no fabricated candle fills or silent cross-roll stitching.
- Downloads can return very short, **nonempty** one-day HMDS responses; currently the downloader advances next_cursor_utc even for these responses. This is a possible contributor to the seven materially incomplete NQ contracts identified on 2026-10-23. Do not equate successful API status or already_complete checkpoint with valid continuous 1m coverage.
- Do NOT automatically change resume pointers, delete historical chunks, change request pacing, or overwrite original Parquet. Generate explicit read-only audit reports. For a repair, implement independent calendar/time-window checks and append-only recovery with guardrail tests, after confirming the root cause.
- Special CME holiday early closes/halts cannot be inferred fully from standard Globex weekday schedules. Never report heuristic short-response flags as proven market-data errors.

## Confidentiality / CI
- Exclude original data, raw Parquet, all logs, checkpoints, credentials, TWS session information, tokens and proprietary market data from version control.
- CI needs only pandas, pytest and tzdata, never brokerage access.
- Do not remove source historical code to make tests pass; add fake-IB fixtures when improving live download logic.

## Separate concerns
- Download (this repo): hh4832/IBKR-market-data-download.
- QA: hh4832/IBKR-market-data-cleaning-pipeline. Do not copy or modify QA data and outputs in downloader.
