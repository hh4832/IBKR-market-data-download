"""Read-only inspection of IBKR requests/checkpoints, never TWS or data mutation.

Partial nonempty HMDS responses may have advanced the historical checkpoint.
Standard exchange hours are heuristic; holidays/halts require separate review.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from ibkr_history_core import (
    is_regular_globex_minute_chicago,
    is_stock_extended_minute_new_york,
)


@dataclass(frozen=True)
class AuditConfig:
    min_expected_minutes: int = 180
    short_response_threshold: float = 0.5

    def validate(self):
        if self.min_expected_minutes < 1 or not 0 < self.short_response_threshold < 1:
            raise ValueError("Invalid audit threshold")


def expected_standard_minutes(window_end_utc: datetime, kind: str = "FUT") -> int:
    """Count regular standard minutes within [end-1day, end), no holiday overrides."""
    if window_end_utc.tzinfo is None:
        raise ValueError("UTC window end must be timezone aware")
    end = pd.Timestamp(window_end_utc).tz_convert("UTC")
    grid = pd.date_range(end - pd.Timedelta(days=1), end, inclusive="left", freq="min")
    if kind == "FUT":
        return sum(is_regular_globex_minute_chicago(t) for t in grid)
    if kind in {"STK", "IND"}:
        return sum(is_stock_extended_minute_new_york(t, index=(kind == "IND")) for t in grid)
    raise ValueError(f"Unsupported type {kind}")


def read_jsonl(path: Path) -> tuple[list[dict], int]:
    records, malformed = [], 0
    if not path.is_file():
        return records, malformed
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            v = json.loads(line)
            if not isinstance(v, dict):
                malformed += 1
            else:
                records.append(v)
        except json.JSONDecodeError:
            malformed += 1
    return records, malformed


def inspect_contract(folder: Path, config: AuditConfig = AuditConfig()) -> tuple[dict, list[dict]]:
    """Summarize saved request records and checkpoint without editing either."""
    config.validate()
    entries, malformed = read_jsonl(folder / "requests.jsonl")
    checkpoint_path = folder / "checkpoint.json"
    if checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            checkpoint = {"parse_error": True}
    else:
        checkpoint = None
    symbol = folder.parent.name
    kind = "FUT" if symbol in {"NQ", "ES"} else "STK"
    suspects = []
    unique_ends = set()
    for x in entries:
        text = x.get("end_utc")
        if not text:
            continue
        try:
            end = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
            expected = expected_standard_minutes(end, kind)
        except (ValueError, TypeError, OverflowError):
            suspects.append({"contract":folder.name, "window_end_utc":text,
                             "status":"MALFORMED_REQUEST_TIME", "classification":"SOURCE_LOG_REVIEW"})
            continue
        unique_ends.add(end.isoformat())
        status, rows = x.get("status"), x.get("rows")
        if status == "weekly_closed_unqueried":
            continue
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            suspects.append({"contract":folder.name, "window_end_utc":end.isoformat(),
                             "status":status, "classification":"BAD_ROW_COUNT", "observed_rows":rows})
            continue
        if expected >= config.min_expected_minutes and rows < expected*config.short_response_threshold:
            suspects.append({"contract":folder.name, "window_end_utc":end.isoformat(),
                             "status":status, "observed_rows":rows,
                             "regular_expected_minutes":expected,
                             "ratio":round(rows/expected,6),
                             "classification":"SUSPECT_SHORT_HMDS_RESPONSE_CHECK_HOLIDAYS_AND_BOUNDARY"})
    chunks = list((folder/"chunks").glob("*.parquet")) if (folder/"chunks").exists() else []
    summary = {
        "symbol":symbol, "contract":folder.name, "request_rows":len(entries),
        "unique_request_window_ends":len(unique_ends),
        "malformed_jsonl_lines":malformed, "chunk_files":len(chunks),
        "has_consolidated":(folder/"consolidated.parquet").exists(),
        "has_checkpoint":checkpoint is not None,
        "checkpoint_next_cursor_utc":checkpoint.get("next_cursor_utc") if checkpoint else None,
        "checkpoint_coverage_mode":checkpoint.get("coverage_mode") if checkpoint else None,
        "suspicious_request_windows":len(suspects),
        "warning":"REQUEST_LOG_COVERAGE_ONLY_NOT_CME_HOLIDAY_VERIFIED",
    }
    return summary, suspects


def scan_dataset(data_root: Path, config: AuditConfig = AuditConfig()) -> tuple[pd.DataFrame,pd.DataFrame]:
    root = data_root/"raw"
    if not root.is_dir():
        raise FileNotFoundError(f"Original raw data directory missing: {root}")
    summaries,flags=[],[]
    for sym in sorted(p for p in root.iterdir() if p.is_dir()):
        for folder in sorted(p for p in sym.iterdir() if p.is_dir()):
            if (folder/"requests.jsonl").is_file() or (folder/"checkpoint.json").is_file():
                stat,suspects=inspect_contract(folder,config)
                summaries.append(stat)
                flags.extend(suspects)
    return pd.DataFrame(summaries),pd.DataFrame(flags)


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description="Offline IBKR request and checkpoint audit (read-only)")
    parser.add_argument("--data-root",type=Path,required=True)
    parser.add_argument("--report-dir",type=Path,default=Path("offline_audit_results"))
    parser.add_argument("--threshold",type=float,default=0.5)
    args=parser.parse_args(argv)
    source=args.data_root.resolve(strict=True)
    dest=args.report_dir.resolve()
    if dest == source or source in dest.parents or dest in source.parents:
        parser.error("Report directory may not be inside or contain original data")
    summary,flags=scan_dataset(source,AuditConfig(short_response_threshold=args.threshold))
    dest.mkdir(parents=True,exist_ok=True)
    summary.to_csv(dest/"contract_request_audit.csv",index=False)
    flags.to_csv(dest/"suspect_request_windows.csv",index=False)
    print(f"Contracts: {len(summary)}; suspicious request windows: {len(flags)}")
    print(f"Offline report: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
