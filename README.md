# IBKR Market Data Download

Windows-local TWS / IB Gateway historical 1-minute data downloader for NQ / ES futures and TSM / SOXX US stocks.

## NQ/ES partial-window fix and separate recovery

The demonstrated 60/120-bar midnight-window bug now fails closed before the
checkpoint advances. NQ/ES requests use explicit elapsed seconds; existing holes
need an independent, calendar-reviewed recovery plan, not a checkpoint reset.
See [evidence, intentional v1.5 changes, preview and bounded recovery](docs/NQ_RECOVERY.md).
The live comparison/probe is pending; keep the repair under review before bulk use.

This repository preserves the recovered original Windows Jupyter downloader (`ibkr_history_core.py`, `IBKR_NQ_ES_1min_Backfill.ipynb`) and the v1.5 watchdog (`ibkr_backfill_watchdog.py`). **It is not a Colab downloader**, does not place orders, and must not be run from GitHub Actions.

## Safe migration: reuse existing history without any IBKR download

**Version note (2026-10-24):** The baseline core was the true Windows v1.5 (53,345 bytes), imported byte-for-byte from the authorized local source. The window-quality fix above is an intentional subsequent change documented in [NQ recovery](docs/NQ_RECOVERY.md). The watchdog still matches the import. The older notebook remains for reference; use `IBKR_NQ_ES_TSM_SOXX_1min_Backfill_v1_5.ipynb` for matching settings. Historical source hashes and notebook changes remain in the [v1.5 import record](docs/V15_IMPORT.md). No market data is included or modified.

The original data is stored here on the user's Windows machine:

```text
C:\Users\hh483\Downloads\00 量化交易研究\05 IBKR 測試\IBKR_MarketData_1min_Backfill_v1_1\IBKR_MarketData
```

Older Windows versions v1.3–v1.5 also refer to this same original data path. The location under `v1_1` **does not mean that the dataset only belongs to v1.1**.

First clone the repository into a **separate** program directory. In **Anaconda Prompt**, with existing `py311` (no installation needed to copy files):

```bat
conda activate py311
cd /d "%USERPROFILE%\Documents"
git clone https://github.com/hh4832/IBKR-market-data-download.git
cd IBKR-market-data-download
mkdir "%USERPROFILE%\Documents\IBKR_Data_Sandbox"
```

If already cloned, use `cd /d "%USERPROFILE%\Documents\IBKR-market-data-download"` and `git pull --ff-only` rather than cloning again.

**Preview (no file writes):**

```bat
python copy_existing_history.py --source "C:\Users\hh483\Downloads\00 量化交易研究\05 IBKR 測試\IBKR_MarketData_1min_Backfill_v1_1\IBKR_MarketData" --dest "C:\Users\hh483\Documents\IBKR_Data_Sandbox\IBKR_MarketData"
```

Check that the source, target, number of files and bytes match expectations, that the old downloader is stopped, and that there is sufficient free disk space. Then **explicitly** copy, with per-file SHA256 verification before the target directory becomes visible:

```bat
python copy_existing_history.py --source "C:\Users\hh483\Downloads\00 量化交易研究\05 IBKR 測試\IBKR_MarketData_1min_Backfill_v1_1\IBKR_MarketData" --dest "C:\Users\hh483\Documents\IBKR_Data_Sandbox\IBKR_MarketData" --execute
```

You can independently recheck both folders afterward:

```bat
python copy_existing_history.py --source "C:\Users\hh483\Downloads\00 量化交易研究\05 IBKR 測試\IBKR_MarketData_1min_Backfill_v1_1\IBKR_MarketData" --dest "C:\Users\hh483\Documents\IBKR_Data_Sandbox\IBKR_MarketData" --verify-only
```

Or open the UI-like notebook `jupyter notebook IBKR_HISTORY_COPY.ipynb` and edit two Windows paths. Its `COPY_NOW=False` default means running all cells **does not copy or download anything**.

The tool copies **all files and empty directories** under the dataset, including each `raw/<symbol>/<contract>/chunks/`, `consolidated.parquet`, `requests.jsonl`, `checkpoint.json`, monitors, coverage reports, `download_plan.csv`, `run_summary.csv`, `contract_discovery.csv` and `head_timestamp_cache.json`. It refuses existing destinations (even empty ones), nested paths, symlinks/junctions within the dataset and special files. It writes only a temporary staging folder under the destination's parent, verifies file sizes and SHA256 against the original, and renames it to the final destination on success. If source files change during the operation, the operation fails. **Stop all existing downloading and syncing jobs during the snapshot.**

The snapshot reuses already downloaded bytes and checkpoint state: it does **not** correct old missing minutes, roll contract gaps, or faulty checkpoint logic. Set a future updated downloader's `DATA_ROOT` **explicitly** to `C:\Users\hh483\Documents\IBKR_Data_Sandbox\IBKR_MarketData` only after verifying that its core and notebook are version-consistent and after creating an independent `ibkr_dl_311` Conda environment. Do not start a full download on that copy until the gap repair plan is approved.

## Windows downloader environment

Run TWS or IB Gateway on the **same Windows computer**. Port `127.0.0.1:7496` in the original Notebook is a configurable example, not proof the selected TWS mode is live or paper. A separate Conda environment is safer because existing `py311` has pandas 3.x / pyarrow 26, outside current repository constraints.

```bat
conda create -n ibkr_dl_311 python=3.11 pip
conda activate ibkr_dl_311
cd /d "%USERPROFILE%\Documents\IBKR-market-data-download"
python -m pip install -r requirements.txt
python -m pip check
```

The original `IBKR_NQ_ES_1min_Backfill.ipynb` is preserved for historical reference and includes **actual smoke-download operations**. Never use Run All on it without reviewing the paths, client ID, and write behavior.

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

## v1.5 planning changes

- Stocks/ETFs default to the last ten calendar years, constrained by available head timestamps; `equity_lookback_years=None` disables that cap.
- Expired futures default to `pre_expiry_session`, omitting the final expiry session and recording excluded windows plus a coverage notice. `calendar_expiry` is an explicit alternative. The predecessor uses weekends only, not an exchange holiday calendar.
- Pacing, original chunk layout and checkpoint schema are unchanged. Existing historical gaps are not repaired by this import; completion statuses still do not prove coverage.
- The v1.5 notebook has cleared outputs, no automatic installation, a separate snapshot path and `ALLOW_CONNECT=False`, `ALLOW_DOWNLOAD=False`. Review the unlimited monitored-run cell before opting in. Do not run against original history.
- Existing py311 was inspected and used without changing installed packages. Its pandas 3 / pyarrow 26 are outside the supported requirements; local results on that environment are supplementary to the Python 3.11 CI run using pandas 2.
- An isolated local Python 3.11 environment with pandas 2.3.3 / pyarrow 24 passed 66 offline tests; one Windows symlink test was skipped for OS permissions. See the import record for validation details.
