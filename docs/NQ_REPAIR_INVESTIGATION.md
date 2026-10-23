# NQ seven-contract incompleteness — investigation plan (no repair applied)

Evidence: 2026-10-23 FULL validation archived at
https://drive.google.com/drive/folders/1gqBm5eF9C0J3IpaIdrXHFSU2tdSbosEQ

Nine quarterly contracts have structurally sound Parquet. Seven have 2024–2026 sessions
where only 60–120 1-minute candles survive many weekdays. Two (NQM6 and NQZ6)
are near-complete after review of market holidays. Because backfill runs and QC are
separate, do not infer that a user-facing summary saying "already_complete" means valid coverage.

## Suspected mechanism to investigate

The original one-day request uses
\`ib.reqHistoricalDataAsync(... durationStr="1 D", barSizeSetting="1 min", ...)\`.
It then restricts timestamps to [window_end-1day, window_end), and when the returned
DataFrame is nonempty, it saves a chunk and advances the checkpoint cursor by 1 day.
**It does not require an expected regular-session bar count before cursor advance.**

This is a plausible source of systematic partial trading sessions, but requires
checking actual \`requests.jsonl\`, \`chunks\`, request endpoints, returned first/last
timestamps, IBKR permissions and HMDS responses. It is not proven by QC alone.
Request pacing / per-contract caps can pause the run, but do not themselves
explain a 22-hour hole on a completed day.

## No-destructive validation sequence

1. Back up the original Windows data directory and \`checkpoint.json\` files.
2. Use \`python ibkr_backfill_audit.py --data-root <ORIGINAL> --report-dir <SEPARATE>\`.
3. Cross-reference \`suspect_request_windows.csv\` with \`requests.jsonl\`,
   \`reports/quality_by_chunk.jsonl\`, \`chunks/end_*.parquet\`,
   \`download_plan.csv\`, \`run_summary.csv\`, and any saved HMDS errors.
4. Verify 1-D request semantics and source timestamp windows using a tiny **single-date**
   real-TWS probe, with permission/expiry limits documented. Preserve exact conId,
   whatToShow, useRTH, durationStr, bar_size, endDateTime, and returned min/max timestamps.
5. Define a separate, explicit repair plan that identifies only confirmed missing time
   slices, emits NEW immutable chunks or a dedicated correction namespace, never changes
   existing checkpoint state without documented migration, and logs source provenance.
6. Re-run the independent \`IBKR-market-data-cleaning-pipeline\` after restoration.

**Do not simply rerun old FULL:** if \`next_cursor_utc <= planned_start_utc\`, it
returns \`already_complete\` without revisiting past partial one-day HMDS windows.
Also do not change \`coverage_mode\` against a saved checkpoint in-place.
IBKR does not guarantee access to arbitrarily old expired futures.
