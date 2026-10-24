# True Windows v1.5 source import — 2026-10-24

Baseline: `434a6d5` on clean `main`, remote
`https://github.com/hh4832/IBKR-market-data-download.git`.

The authorized local source was inventoried read-only. File sizes and SHA256
hashes are in [the source manifest](v15_source_manifest.json). No original
dataset, Parquet, chunks, checkpoints or download logs were copied or changed.
No TWS/Gateway connection or market-data request was made.

## Source and behavior

- `ibkr_history_core.py`: imported byte-for-byte, 53,345 bytes,
  SHA256 `a7566d473e984807514b81f3e99d59393d5c81276d9c76bcba58ce62ea7b3630`,
  Git blob `e9bde8886384d2bb84bb85531d360ef896020051`.
  Baseline v1.4 blob: `9cecfb414d7869cdd0c3c6e107851be41cd1b0eb`.
- `ibkr_backfill_watchdog.py`: matches local source after LF normalization
  (the Windows checkout uses CRLF), no changes;
  Git blob `3a82a1c0becbfa57ea420ab1fa5360afc2da93b2`.
- `test_ibkr_history_core.py`: imported unchanged as `tests/test_v15_source.py`.
  Fake requesters and temporary data exercise planning, checkpoints, resume,
  per-contract failure isolation and v1.5 boundaries. Some tests explicitly
  substitute pickle for Parquet; they do not validate real Parquet serialization.
- Source notebook imported under its v1.5 filename, with intentional safety
  changes: stripped outputs/execution metadata; removed automatic installation;
  selected a separate history-copy path; added false-by-default connection and
  download gates, including the originally unconditional monitored run.
  That monitored run retains unlimited budgets when explicitly enabled.
  All other research settings and the older original notebook are retained.
- Added `ipywidgets>=8,<9` to notebook dependencies to match the v1.5 source.
  Existing package upper bounds remain supported constraints.
- Existing copy utility, copy notebook, audit, tests and investigation remain.

v1.5 adds a configurable ten-calendar-year equity floor and a default
`pre_expiry_session` cap for expired futures, recording excluded terminal
windows and coverage notices. The cap uses a weekend-only predecessor, not a
complete exchange holiday calendar. `calendar_expiry` remains opt-in. This
import does not repair incomplete historical responses or reset original
checkpoints. A capped plan and an `already_complete` status do not prove coverage.

## Validation

Python 3.11.11 in the existing `py311` environment was inspected without
installing, upgrading or downgrading packages. It has pandas 3.0.6 and pyarrow
26.0.0, outside the repository's pandas<3 / pyarrow<25 bounds. Its offline test
results are supplementary; supported-range validation runs in Python 3.11 CI
with pandas 2, pytest and tzdata only. No brokerage dependency is needed by CI.

Final Windows validation used an isolated repository `.venv` with Python 3.11.11,
pandas 2.3.3, pyarrow 24.0.0, pytest 9.1.1 and tzdata 2026.5: **66 passed,
1 skipped** (Windows symlink creation permission). Real synthetic Parquet
round-trip, exclusion CSV/summary propagation, notebook syntax/API/default
execution checks and all existing tests passed. All notebook schemas were also
validated with nbformat in the unchanged py311 environment. Python compilation
and Git whitespace checks passed. GitHub Actions status is available on the
import commit's Checks page; local results are not a substitute for that status.

Notebook checks compile Python cells with top-level await support and validate
v1.5 imports/Settings keyword compatibility. The v1.5 default Run All test uses
a temporary home directory, a fake IB constructor, forbidden download/sync
functions and connection guards. Existing notebooks are syntax-checked only;
their original live-operation cells are not executed.

Tests deny outbound socket connections. On Windows only standard-library
construction of asyncio's internal socket pair is exempted. Test data stays
in ignored temporary folders within this checkout. No downloaded source
notebook outputs or proprietary data are committed.

The tests replace the unconditional `tqdm.auto` import with a fixture that fails
if progress is actually invoked; fake orchestration tests disable progress.
This preserves the CI dependency boundary without editing recovered source.
