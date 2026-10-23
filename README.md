# IBKR Market Data Download

Windows-local TWS / IB Gateway historical 1-minute data downloader for NQ / ES futures and TSM / SOXX US stocks.

This repository preserves the recovered original Windows Jupyter downloader (`ibkr_history_core.py`, `IBKR_NQ_ES_1min_Backfill.ipynb`) and the v1.5 watchdog (`ibkr_backfill_watchdog.py`). **It is not a Colab downloader**, does not place orders, and must not be run from GitHub Actions.

## Windows quick start (Python 3.11)

1. Open TWS or IB Gateway on the **same Windows desktop**, log in, enable read-only API, and verify the correct socket port. The recovered Notebook defaults to **127.0.0.1:7496** (commonly TWS live, not universal).
2. In Windows PowerShell:

```powershell
git clone https://github.com/hh4832/IBKR-market-data-download.git
cd IBKR-market-data-download
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
jupyter notebook IBKR_NQ_ES_1min_Backfill.ipynb
```

3. Configure `cfg.host`, `cfg.port`, `cfg.client_id`, `DATA_ROOT`, and optional Google Drive **for desktop local sync path**. Use your **existing** `DATA_ROOT` to resume earlier checkpoint files. A Drive folder link alone is not a Windows filesystem path.
4. Start `smoke_test=True`; then explicitly set `RUN_FULL=True` after checking connectivity and market-data permissions. Read `requests.jsonl`, `checkpoint.json`, `reports/` and chunk files after each run.

### Data quality finding (2026-10-23)

The separate [NQ Globex Full QC report](https://drive.google.com/drive/folders/1gqBm5eF9C0J3IpaIdrXHFSU2tdSbosEQ) indicates only ~39.9% minute coverage across nine quarterly contracts. Seven contracts show many days with only 60–120 bars. **Treat this as a source-data completeness issue until resolved**. The recovered downloader can advance a daily checkpoint after an incomplete but nonempty API response; an `already_complete` checkpoint is not proof that minute coverage is complete.

Use the new `ibkr_backfill_audit.py` in read-only mode to inspect original request logs and identify suspicious short responses before changing historical data:

```powershell
python ibkr_backfill_audit.py --data-root "C:\\path\\to\\IBKR_Futures" --report-dir ".\\offline_audit_results"
```

The script never modifies raw Parquet, chunks or checkpoint files. Its expected-minute counts are standard weekly CME/NYSE *heuristics*, not an official holiday calendar. Do not simply reset or delete historical checkpoints.

### Repository roles

- `ibkr_history_core.py`: preserved original `ib_async` downloader, one-day requests, incremental checkpoint and per-contract Parquet chunks.
- `IBKR_NQ_ES_1min_Backfill.ipynb`: preserved Windows Jupyter operator Notebook.
- `ibkr_backfill_watchdog.py`: optional socket/market-data farm status monitor.
- `ibkr_backfill_audit.py`: new, offline read-only diagnostics.
- `tests/`, `.github/workflows/tests.yml`: offline tests only, no real TWS socket access.

**Do not commit** data, API keys, tokens, credentials, Parquet or checkpoints. All contracts retain `conId`, `localSymbol`, expiry, `timestamp_utc`, `whatToShow=TRADES`, `useRTH=False`. No continuous NQ stitching or inferred roll-adjustment is performed. Archived data remains in the [NQ Drive folder](https://drive.google.com/drive/folders/1C0wfYpT8zioRmZxBnqO7d79-zJyD_S5w); quality checks belong in [IBKR-market-data-cleaning-pipeline](https://github.com/hh4832/IBKR-market-data-cleaning-pipeline).

Market-data usage and redistribution remain subject to IBKR/exchange permissions.
