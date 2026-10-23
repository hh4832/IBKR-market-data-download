"""Research-grade (not exchange-certified) NQ/ES futures and TSM/SOXX stock backfiller.
Designed for asynchronous use in local Windows Jupyter with ib_async.
No order placement methods are used.
"""
from __future__ import annotations

import asyncio
import calendar
from collections import deque
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import os
import shutil
import time
from typing import Optional

import pandas as pd

UTC = timezone.utc
BAR_COLUMNS = ["timestamp_utc", "open", "high", "low", "close", "volume", "average", "barCount",
               "symbol", "localSymbol", "expiry", "conId", "exchange", "bar_size", "what_to_show", "use_rth"]


@dataclass
class Settings:
    symbols: tuple[str, ...] = ("NQ", "ES")
    stocks: tuple[str, ...] = ("TSM", "SOXX")
    # Index symbols are *disabled* until historical 1m bars are verified in a probe.
    indices: tuple[str, ...] = ()
    host: str = "127.0.0.1"
    port: int = 7496
    client_id: int = 22
    output_dir: str = "IBKR_Futures"
    # Path to a *local* Google Drive for desktop sync folder; None means manual upload.
    google_drive_dir: Optional[str] = None
    smoke_test: bool = True
    smoke_requests_per_contract: int = 3
    coverage_mode: str = "roll_window"  # 'roll_window' (recommended) or 'full_contract'
    overlap_days: int = 14
    # The first accessible contract is retrieved for 105 days before expiration.
    first_contract_days: int = 105
    # Large to avoid an artificial historical-data cutoff; old expiries are constrained by IBKR.
    full_contract_max_days: int = 1825
    # Applies conservative pacing even though 1min bars are exempt from older hard small-bar limits.
    min_interval_seconds: float = 12.0
    requests_per_10_minutes: int = 45
    max_retries: int = 3
    timeout_seconds: float = 75.0
    contract_details_interval: float = 0.65
    # Set True for explicit, limited recent catch-up; does not replace complete historical backfill.
    refresh_only: bool = False
    refresh_days: int = 7
    # Maximum new historical requests across all instruments in each notebook run.
    # Use None for unrestricted backfill; 150 ~= 30 minutes at 12s/request.
    max_requests_per_run: Optional[int] = 150
    # When True, fail if the head timestamp is unavailable. Default False: fallback
    # to a deliberately conservative calendar floor and mark coverage UNVERIFIED.
    equity_head_required: bool = False
    equity_fallback_year: int = 1990
    show_progress: bool = True
    # A single unavailable/ambiguous contract window must not abort other contracts.
    # Original cursor/checkpoint is retained and the contract is marked for review.
    continue_on_contract_error: bool = True
    max_requests_per_contract_per_run: Optional[int] = 5
    # If IBKR did not return a verified head timestamp, stop automatically only
    # after sustained *weekday* no-data replies AFTER previously downloaded bars.
    # This is a pragmatic boundary, not proof that earlier data cannot exist.
    stop_on_unverified_boundary: bool = True
    equity_empty_weekdays_to_stop: int = 10
    equity_head_cache_days: int = 7

    def validate(self) -> None:
        if not set(self.symbols).issubset({"NQ", "ES"}):
            raise ValueError("Futures symbols must be a subset of NQ, ES.")
        if not set(self.stocks).issubset({"TSM", "SOXX"}):
            raise ValueError("Supported stocks: TSM, SOXX.")
        if not set(self.indices).issubset({"SOX", "SDL"}):
            raise ValueError("Supported optional index probes: SOX, SDL.")
        if not (self.symbols or self.stocks or self.indices):
            raise ValueError("Select at least one symbol.")
        if self.max_requests_per_contract_per_run is not None and self.max_requests_per_contract_per_run < 1:
            raise ValueError("max_requests_per_contract_per_run must be >=1 or None")
        if self.max_requests_per_run is not None and self.max_requests_per_run < 1:
            raise ValueError("max_requests_per_run must be >=1 or None")
        if self.coverage_mode not in {"roll_window", "full_contract"}:
            raise ValueError("coverage_mode must be roll_window or full_contract")
        if self.port <= 0 or self.min_interval_seconds < 0 or self.requests_per_10_minutes <= 0:
            raise ValueError("Invalid API/rate settings")
        if self.refresh_days < 1 or self.smoke_requests_per_contract < 1:
            raise ValueError("Refresh and smoke periods must be positive")
        if self.equity_empty_weekdays_to_stop < 3:
            raise ValueError("equity_empty_weekdays_to_stop must be >=3")
        if self.equity_head_cache_days < 1:
            raise ValueError("equity_head_cache_days must be >=1")
        if not (1900 <= self.equity_fallback_year <= utc_now().year):
            raise ValueError("equity_fallback_year must be in a valid historical range")


def utc_now() -> datetime:
    return datetime.now(UTC)


def most_recent_futures_trading_end_utc(now: datetime) -> datetime:
    """Avoid starting an intraday history request in a known CME weekend/maintenance closure.
    Not an exchange holiday calendar; historical data is still verified per returned bars.
    """
    chicago = ZoneInfo("America/Chicago")
    local = now.astimezone(chicago)
    wd = local.weekday()
    # Sat, or Sunday before Globex 17:00 CT -> most recent Friday 16:00 CT.
    if wd == 5 or (wd == 6 and local.hour < 17):
        days_since_friday = (wd - 4) % 7
        friday = local.date() - timedelta(days=days_since_friday)
        return datetime.combine(friday, datetime.min.time(), chicago).replace(hour=16).astimezone(UTC)
    # Weekday maintenance Friday after close or Mon-Thu between 16:00 and 17:00.
    if wd < 5 and local.hour >= 16 and (wd == 4 or local.hour < 17):
        return local.replace(hour=16, minute=0, second=0, microsecond=0).astimezone(UTC)
    return now


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7 + 14)


def candidate_quarter_months(now: datetime, history_years: int = 4) -> list[str]:
    """Generate candidates, intentionally scanning beyond normal IBKR expired-future availability."""
    end_year = now.year + 1
    return [f"{y}{m:02d}" for y in range(now.year-history_years, end_year + 1)
            for m in (3, 6, 9, 12)]


def parse_utc(v) -> datetime:
    x = pd.Timestamp(v)
    if x.tzinfo is None:
        raise ValueError(f"Naive timestamp is disallowed: {v}")
    return x.tz_convert("UTC").to_pydatetime()


def expiry_of(contract) -> date:
    return datetime.strptime(contract.lastTradeDateOrContractMonth[:8], "%Y%m%d").date()


async def discover_quarterly_contracts(ib, settings: Settings, now: Optional[datetime] = None) -> tuple[list, pd.DataFrame]:
    """Resolve NQ/ES quarterly futures as actual dated contracts, not CONTFUT.

    Expired futures are usually unavailable >2 years after expiry. Record skipped
    candidates so users can see the policy cutoff is not a promise of availability.
    """
    from ib_async import Future
    now = now or utc_now()
    details_log = []
    found = []
    for symbol in settings.symbols:
        for ym in candidate_quarter_months(now):
            y, m = int(ym[:4]), int(ym[4:])
            exp_est = third_friday(y, m)
            if exp_est < (now.date() - timedelta(days=730)):
                details_log.append(dict(symbol=symbol, quarter=ym, status="skip_expired_over_2y", conId=None))
                continue
            if exp_est > (now.date() + timedelta(days=195)):
                continue
            candidate = Future(symbol=symbol, lastTradeDateOrContractMonth=ym, exchange="CME", currency="USD")
            candidate.includeExpired = True
            try:
                results = await asyncio.wait_for(ib.reqContractDetailsAsync(candidate), timeout=30)
            except Exception as exc:
                # Fail closed on API errors: an outage should not silently hide a contract.
                raise RuntimeError(f"Contract discovery failed for {symbol} {ym}: {exc}") from exc
            matches = [r.contract for r in results
                       if r.contract.symbol == symbol
                       and str(r.contract.lastTradeDateOrContractMonth).startswith(ym)
                       and r.contract.secType == "FUT"]
            if len(matches) == 1:
                c = matches[0]
                c.includeExpired = True
                found.append(c)
                details_log.append(dict(symbol=symbol, quarter=ym, status="resolved",
                                        conId=c.conId, localSymbol=c.localSymbol,
                                        expiry=c.lastTradeDateOrContractMonth))
            elif not matches:
                details_log.append(dict(symbol=symbol, quarter=ym, status="contract_not_found", conId=None))
            else:
                # Ambiguous contract definitions should not be selected arbitrarily.
                details_log.append(dict(symbol=symbol, quarter=ym, status="ambiguous", conId=None))
                raise RuntimeError(f"Ambiguous contract for {symbol} {ym}: {len(matches)} matches")
            await asyncio.sleep(settings.contract_details_interval)
    found.sort(key=lambda c: (c.symbol, expiry_of(c)))
    return found, pd.DataFrame(details_log)


def future_last_trade_window_end_utc(last_trading_day: date) -> datetime:
    """Exclusive UTC end cap for an expired quarterly future.

    Contract.lastTradeDateOrContractMonth is the authoritative last trade DATE,
    not a promise that quotes exist all the way to midnight. We allow the entire
    *UTC calendar date* of the last trading day, but never request dates after it.
    The last intraday session can have a shorter schedule (e.g., Juneteenth).
    """
    return datetime.combine(last_trading_day + timedelta(days=1), datetime.min.time(), UTC)


def build_download_plan(contracts: list, settings: Settings, now: Optional[datetime] = None) -> pd.DataFrame:
    """Per-contract bounded windows. roll_window preserves overlapping raw contracts.
    It does NOT create/adjust a continuous futures series or declare a roll date.
    """
    now = now or utc_now()
    rows = []
    for symbol in settings.symbols:
        selected = sorted((c for c in contracts if c.symbol == symbol), key=expiry_of)
        prior_expiry = None
        for c in selected:
            expiry = expiry_of(c)
            if settings.coverage_mode == "roll_window":
                start_day = ((prior_expiry - timedelta(days=settings.overlap_days))
                             if prior_expiry else expiry - timedelta(days=settings.first_contract_days))
            else:
                start_day = expiry - timedelta(days=settings.full_contract_max_days)
            end_dt = min(most_recent_futures_trading_end_utc(now),
                         future_last_trade_window_end_utc(expiry))
            start_dt = datetime.combine(start_day, datetime.min.time(), UTC)
            rows.append(dict(symbol=symbol, localSymbol=c.localSymbol, conId=int(c.conId),
                             expiry=expiry.isoformat(), contract=c, start_utc=start_dt,
                             end_utc=end_dt, eligible=start_dt < end_dt))
            prior_expiry = expiry
    return pd.DataFrame(rows)


def filter_eligible_plan(plan: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Exclude future-start plans from download and visible work estimates.

    Return (eligible, ineligible), retaining the latter for diagnostics.
    """
    if plan.empty:
        return plan.copy(), plan.copy()
    good = plan["eligible"].astype(bool) & (plan["start_utc"] < plan["end_utc"])
    selected = plan.loc[good].copy()
    skipped = plan.loc[~good].copy()
    if not skipped.empty:
        skipped["skip_reason"] = "start_not_before_end_or_not_eligible"
    return selected, skipped


def fair_contract_order(plan: pd.DataFrame) -> pd.DataFrame:
    """Round-robin instruments; newest futures quarter first within each."""
    if plan.empty:
        return plan.copy()
    grouped = {
        symbol: group.sort_values("end_utc", ascending=False).reset_index(drop=True)
        for symbol, group in plan.groupby("symbol", sort=True)
    }
    symbols = sorted(grouped)
    rows = []
    for i in range(max(len(grouped[s]) for s in symbols)):
        for sym in symbols:
            if i < len(grouped[sym]):
                rows.append(grouped[sym].iloc[i].to_dict())
    return pd.DataFrame(rows).reset_index(drop=True)


class HistoryPacer:
    def __init__(self, minimum_interval: float = 12.0, rolling_limit: int = 45, window_s: float = 600.0):
        self.minimum_interval = minimum_interval
        self.rolling_limit = rolling_limit
        self.window_s = window_s
        self._requests: deque[float] = deque()

    async def wait(self) -> None:
        while True:
            now = time.monotonic()
            while self._requests and now - self._requests[0] >= self.window_s:
                self._requests.popleft()
            delay = 0.0
            if self._requests:
                delay = max(delay, self.minimum_interval - (now - self._requests[-1]))
            if len(self._requests) >= self.rolling_limit:
                delay = max(delay, self.window_s - (now - self._requests[0]) + 0.25)
            if delay <= 0:
                self._requests.append(time.monotonic())
                return
            await asyncio.sleep(delay)


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")


def bars_to_frame(bars, contract) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=BAR_COLUMNS)
    from ib_async import util
    df = util.df(bars)
    df["timestamp_utc"] = pd.to_datetime(df.pop("date"), utc=True)
    df["symbol"] = contract.symbol
    df["localSymbol"] = getattr(contract, "localSymbol", "") or contract.symbol
    df["expiry"] = getattr(contract, "lastTradeDateOrContractMonth", "")
    df["conId"] = int(contract.conId)
    df["exchange"] = contract.exchange
    df["bar_size"] = "1 min"
    df["what_to_show"] = "TRADES"
    df["use_rth"] = False
    df["secType"] = getattr(contract, "secType", "FUT")
    for col in ["open", "high", "low", "close", "volume", "average", "barCount"]:
        if col not in df:
            df[col] = float('nan')
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in BAR_COLUMNS:
        if col not in df:
            df[col] = pd.NA
    return (df[BAR_COLUMNS + ["secType"]].sort_values("timestamp_utc")
            .drop_duplicates(["conId", "timestamp_utc"], keep="last").reset_index(drop=True))


def write_chunk(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.parquet")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_contract_data(contract_dir: Path) -> pd.DataFrame:
    parts = sorted((contract_dir / "chunks").glob("*.parquet"))
    if not parts:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
    return (df.drop_duplicates(subset=["conId", "timestamp_utc"], keep="last")
            .sort_values("timestamp_utc").reset_index(drop=True))


def validate_frame(df: pd.DataFrame) -> dict:
    if df.empty:
        return dict(rows=0, duplicates=0, null_ohlc=0, invalid_ohlc=0, negative_volume=0,
                    unaligned_minute=0, start_utc=None, end_utc=None)
    ts = pd.to_datetime(df["timestamp_utc"], utc=True)
    o, h, l, c = [pd.to_numeric(df[col], errors="coerce") for col in ["open", "high", "low", "close"]]
    invalid = (h < pd.concat([o,c,l], axis=1).max(axis=1)) | (l > pd.concat([o,c,h],axis=1).min(axis=1))
    return dict(rows=len(df), duplicates=int(ts.duplicated().sum()),
                null_ohlc=int(df[["open", "high", "low", "close"]].isna().sum().sum()),
                invalid_ohlc=int(invalid.sum()),
                negative_volume=int((pd.to_numeric(df["volume"], errors="coerce") < 0).sum()),
                unaligned_minute=int(((ts.dt.second != 0) | (ts.dt.microsecond != 0)).sum()),
                start_utc=ts.min().isoformat(), end_utc=ts.max().isoformat())


def is_regular_globex_minute_chicago(ts: pd.Timestamp) -> bool:
    """Heuristic exchange hours, NOT an official holiday/early-close calendar.

    CME equity index futures generally run Sunday 17:00 CT through Friday 16:00 CT,
    with weekday 16:00-17:00 CT maintenance. Holidays and early closes differ.
    """
    local = ts.tz_convert("America/Chicago")
    wd, hour = local.weekday(), local.hour
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday
        return hour >= 17
    if wd == 4:  # Friday
        return hour < 16
    return hour >= 17 or hour < 16


def is_stock_extended_minute_new_york(ts: pd.Timestamp, index: bool = False) -> bool:
    """US typical hours only; NOT holiday, halt, or verified index-provider schedule."""
    local = ts.tz_convert("America/New_York")
    if local.weekday() >= 5:
        return False
    minute = local.hour * 60 + local.minute
    return (570 <= minute < 960) if index else (240 <= minute < 1200)


def gap_report(df: pd.DataFrame, expand_cap_minutes: int = 10080,
               sec_type: str = "FUT") -> pd.DataFrame:
    cols = ["previous_bar_utc", "next_bar_utc", "gap_minutes", "missing_minutes",
            "likely_trading_minutes", "classification"]
    if len(df) < 2:
        return pd.DataFrame(columns=cols)
    ts = pd.to_datetime(df["timestamp_utc"], utc=True).drop_duplicates().sort_values()
    diffs = ts.diff()
    rows = []
    for i in range(1, len(ts)):
        delta = diffs.iloc[i]
        if pd.isna(delta) or delta <= pd.Timedelta(minutes=1):
            continue
        total = int(delta.total_seconds() / 60)
        missing = max(total - 1, 0)
        if missing > expand_cap_minutes:
            expected = None
            classification = "long_gap_manual_review"
        else:
            grid = pd.date_range(ts.iloc[i-1] + pd.Timedelta(minutes=1),
                                 ts.iloc[i] - pd.Timedelta(minutes=1), freq="1min")
            expected = sum((is_regular_globex_minute_chicago(x) if sec_type == "FUT"
                            else is_stock_extended_minute_new_york(x, index=(sec_type == "IND")))
                           for x in grid)
            classification = ("closed_session_candidate" if expected == 0
                              else "possible_missing_trading_bars_check_holidays")
        rows.append(dict(previous_bar_utc=ts.iloc[i-1].isoformat(),
                         next_bar_utc=ts.iloc[i].isoformat(), gap_minutes=total,
                         missing_minutes=missing, likely_trading_minutes=expected,
                         classification=classification))
    return pd.DataFrame(rows, columns=cols)


def reports_for_contract(contract_dir: Path, automatic_full_limit: int = 120) -> dict:
    chunk_paths = list((contract_dir / "chunks").glob("*.parquet"))
    if len(chunk_paths) > automatic_full_limit:
        # Avoid quadratic repeated read/concat of a multi-decade minute-bar archive.
        # Every new window has its own quality log; full gap audit is explicit later.
        old_consolidated = contract_dir / "consolidated.parquet"
        if old_consolidated.exists():
            old_consolidated.unlink()  # stale consolidated datasets must never be served.
        info = dict(rows=None, validation_scope="per_chunk_only_large_dataset",
                    chunk_count=len(chunk_paths), gaps_count=None, suspect_gaps=None,
                    full_audit_required=True)
        atomic_json(contract_dir / "reports" / "validation.json", info)
        return info
    df = load_contract_data(contract_dir)
    validation = validate_frame(df)
    validation["validation_scope"] = "full_contract_available_chunks"
    gaps = gap_report(df, sec_type=(str(df["secType"].iloc[0]) if "secType" in df and len(df) else "FUT"))
    report_dir = contract_dir / "reports"
    report_dir.mkdir(exist_ok=True, parents=True)
    atomic_json(report_dir / "validation.json", validation)
    gaps.to_csv(report_dir / "gaps.csv", index=False)
    if not df.empty:
        # Derived aggregate is for convenience; immutable original chunks remain.
        write_chunk(contract_dir / "consolidated.parquet", df)
    return {**validation, "gaps_count": len(gaps),
            "suspect_gaps": int(gaps.classification.isin([
                "possible_missing_trading_bars_check_holidays", "long_gap_manual_review"]).sum()) if len(gaps) else 0}


def contract_folder(root: Path, c) -> Path:
    # Keep FUT old paths exactly unchanged for compatible checkpoint resume.
    local = getattr(c, "localSymbol", "") or c.symbol
    return root / "raw" / c.symbol / f"{local}_{c.conId}"


def _fatal_ibkr_message(code: int, message: str) -> bool:
    s = message.lower()
    if any(t in s for t in ("no market data permissions", "not subscribed", "permission", "pacing violation", "not connected", "not allowed")):
        return True
    return code in {200, 321, 354, 420, 502, 504, 10167, 10186, 10225} and "no data" not in s


def is_verified_weekly_closed_window(c, end: datetime) -> bool:
    """Conservative regular WEEKLY closure for a complete [end-1d,end) window.

    Skips only windows in which not a single 30-minute sample is in a potential
    scheduled session; never uses this to infer holiday/early closes. Quarterly
    future expiry is bounded separately in build_download_plan().
    """
    sec_type = getattr(c, "secType", "FUT")
    start = pd.Timestamp(end - timedelta(days=1))
    if start.tzinfo is None:
        raise ValueError("Expected timezone-aware historical request end")
    for interval in range(48):
        ts = start + pd.Timedelta(minutes=interval * 30)
        potential = (is_regular_globex_minute_chicago(ts) if sec_type == "FUT"
                     else is_stock_extended_minute_new_york(ts, index=(sec_type == "IND")))
        if potential:
            return False
    return True


async def request_one_day(ib, c, end: datetime, pacer: HistoryPacer,
                          settings: Settings) -> tuple[pd.DataFrame, list, int]:
    """Return (frame, recorded IB errors, tries). Do not interpret API errors as empty sessions."""
    low = end - timedelta(days=1)
    last_error = None
    all_errors: list[dict] = []
    for attempt in range(1, settings.max_retries + 1):
        await pacer.wait()
        events = []
        def record(req_id, code, message, event_contract):
            events.append({"req_id": req_id, "code": code, "message": str(message)})
        ib.errorEvent += record
        try:
            bars = await ib.reqHistoricalDataAsync(
                c, endDateTime=end, durationStr="1 D", barSizeSetting="1 min",
                whatToShow="TRADES", useRTH=False, formatDate=2,
                keepUpToDate=False, timeout=settings.timeout_seconds)
        except Exception as exc:
            last_error = str(exc)
            bars = []
        finally:
            ib.errorEvent -= record
        all_errors.extend(events)
        fatal = [(x["code"], x["message"]) for x in events if _fatal_ibkr_message(x["code"], x["message"])]
        if fatal:
            last_error = str(fatal)
        if not fatal and bars:
            df = bars_to_frame(bars, c)
            df = df[(df.timestamp_utc >= pd.Timestamp(low)) &
                    (df.timestamp_utc < pd.Timestamp(end))].copy()
            return df, all_errors, attempt
        # Explicit HMDS 'no data' / expected closed market session can return empty.
        no_data = any(any(mark in x["message"].lower() for mark in (
            "no data", "no historical market data", "no historical data")) for x in events)
        if not fatal and no_data:
            return bars_to_frame([], c), all_errors, attempt
        # Blank responses without errors are ambiguous; retry, then checkpoint as unresolved only if fatal absent.
        if attempt < settings.max_retries:
            await asyncio.sleep(min(15 * (2 ** (attempt-1)), 60))
    if last_error or any(_fatal_ibkr_message(x["code"], x["message"]) for x in all_errors):
        raise RuntimeError(f"IBKR data request failed {c.localSymbol} {end}: {last_error or all_errors}")
    raise RuntimeError(f"Ambiguous empty/timeout after retries for {c.localSymbol} {end}; refusing to skip window")


class RequestBudget:
    """Shared across symbols so a single run never exceeds the requested work limit."""
    def __init__(self, max_requests: Optional[int]):
        self.limit = max_requests
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit


async def backfill_one_contract(ib, plan_row, root: Path, settings: Settings,
                                pacer: HistoryPacer, budget: Optional[RequestBudget] = None,
                                progress=None) -> dict:
    c = plan_row["contract"]
    folder = contract_folder(root, c)
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint_file = folder / "checkpoint.json"
    state = json.loads(checkpoint_file.read_text(encoding="utf-8")) if checkpoint_file.exists() else {}
    boundary_file = folder / "unverified_history_boundary.json"
    heuristic_active = bool(
        c.symbol in settings.stocks
        and settings.stop_on_unverified_boundary
        and plan_row.get("head_source") == "fallback_floor_UNVERIFIED"
        and not settings.smoke_test and not settings.refresh_only
    )
    if heuristic_active and boundary_file.exists():
        return dict(symbol=c.symbol, localSymbol=getattr(c, "localSymbol", "") or c.symbol,
                    conId=c.conId, status="boundary_unverified_already_stopped",
                    requests=0, new_rows=0)
    empty_weekdays = int(state.get("consecutive_empty_weekdays", 0)) if heuristic_active else 0
    has_historical_bars = bool(list((folder / "chunks").glob("*.parquet"))) if heuristic_active else False
    planned_start: datetime = plan_row["start_utc"]
    planned_end: datetime = plan_row["end_utc"]
    if settings.refresh_only:
        cursor = min(planned_end, utc_now())
        stop = max(planned_start, cursor - timedelta(days=settings.refresh_days))
    else:
        cursor = parse_utc(state["next_cursor_utc"]) if state.get("next_cursor_utc") else planned_end
        # A previous smoke may have a narrower starting bound. Stop at current plan bound.
        cursor = min(cursor, planned_end)
        stop = planned_start
    if state and (not settings.refresh_only) and state.get("coverage_mode") not in (None, settings.coverage_mode):
        raise RuntimeError(f"Existing checkpoint uses coverage_mode={state.get('coverage_mode')},"
                           f" but settings use {settings.coverage_mode}. Choose a new DATA_ROOT.")
    if cursor <= stop:
        return dict(symbol=c.symbol, localSymbol=getattr(c, "localSymbol", "") or c.symbol,
                    conId=c.conId, status="already_complete", requests=0, new_rows=0)
    requested = 0
    skipped_closed = 0
    new_rows = 0
    status = "complete"
    total_days = max(0, int((cursor - stop).total_seconds() / 86400 + 0.999999))
    bar = progress(total=total_days, desc=f"{c.symbol} {getattr(c, 'localSymbol', '')}",
                   unit="day", leave=False) if progress else None
    try:
        while cursor > stop:
            if settings.smoke_test and requested >= settings.smoke_requests_per_contract:
                status = "paused_smoke_limit"
                break
            if (not settings.smoke_test and settings.max_requests_per_contract_per_run is not None
                    and requested >= settings.max_requests_per_contract_per_run):
                status = "paused_contract_slice"
                break
            if budget is not None and budget.exhausted:
                status = "paused_budget"
                break
            if not ib.isConnected():
                raise RuntimeError("TWS disconnected; checkpoint retained. Rerun after reconnecting.")
            # A complete weekly closed window has zero API requests and cannot
            # legitimately contain regular US futures/stock minutes. The calendar
            # is deliberately conservative: holidays remain unverified.
            weekly_closed = is_verified_weekly_closed_window(c, cursor)
            if weekly_closed:
                frame = bars_to_frame([], c)
                errors, attempts = [], 0
                skipped_closed += 1
            else:
                # Only a completed API request counts toward the budget;
                # failed retries are still controlled by HistoryPacer.
                frame, errors, attempts = await request_one_day(ib, c, cursor, pacer, settings)
            frame = frame[frame.timestamp_utc >= pd.Timestamp(stop)] if not frame.empty else frame
            stem = cursor.strftime("%Y%m%dT%H%M%SZ")
            target = folder / "chunks" / f"end_{stem}.parquet"
            if not frame.empty:
                write_chunk(target, frame)
                new_rows += len(frame)
                quality = validate_frame(frame)
                append_jsonl(folder / "reports" / "quality_by_chunk.jsonl",
                    dict(window_end_utc=cursor.isoformat(), **quality))
            append_jsonl(folder / "requests.jsonl", dict(
                at_utc=utc_now().isoformat(), end_utc=cursor.isoformat(),
                start_utc=(cursor - timedelta(days=1)).isoformat(), rows=len(frame),
                status=("weekly_closed_unqueried" if weekly_closed
                        else "ok" if len(frame) else "no_data_reported"),
                attempts=attempts, errors=errors))
            if not weekly_closed:
                requested += 1
            if heuristic_active:
                if not frame.empty:
                    has_historical_bars = True
                    empty_weekdays = 0
                else:
                    # The 'end' date is UTC; use the relevant US exchange session
                    # day to exclude Sat/Sun. Exchange holidays are treated as
                    # possible empty weekdays; ten consecutive days is conservative.
                    ny = (cursor - timedelta(hours=1)).astimezone(ZoneInfo("America/New_York"))
                    if ny.weekday() < 5:
                        empty_weekdays += 1
            if budget is not None and not weekly_closed:
                budget.used += 1
            # Move exactly one day back; last chunk may partially overlap planned_start.
            cursor -= timedelta(days=1)
            if not settings.refresh_only:
                atomic_json(checkpoint_file, dict(next_cursor_utc=cursor.isoformat(),
                    planned_start_utc=planned_start.isoformat(),
                    initial_end_utc=planned_end.isoformat(),
                    coverage_mode=settings.coverage_mode,
                    consecutive_empty_weekdays=empty_weekdays if heuristic_active else 0,
                    last_updated_utc=utc_now().isoformat()))
            if (heuristic_active and has_historical_bars and
                    empty_weekdays >= settings.equity_empty_weekdays_to_stop):
                # Persist *uncertain* inference separately from the normal cursor:
                # users can opt out to keep looking back, without losing chunks.
                atomic_json(boundary_file, dict(
                    status="unverified_no_data_boundary",
                    symbol=c.symbol, conId=c.conId,
                    stopped_after_window_end_utc=(cursor + timedelta(days=1)).isoformat(),
                    next_cursor_utc=cursor.isoformat(),
                    consecutive_empty_weekdays=empty_weekdays,
                    threshold=settings.equity_empty_weekdays_to_stop,
                    historical_data_nonempty_seen=True,
                    warning="Heuristic stop only; not a confirmed earliest IBKR 1-min timestamp",
                    at_utc=utc_now().isoformat()))
                status = "boundary_unverified_review"
                if bar is not None:
                    bar.update(1)
                print(f"{c.symbol}: stopped after {empty_weekdays} empty US weekdays; "
                      "earliest historical availability remains UNVERIFIED.", flush=True)
                break
            if bar is not None:
                bar.update(1)
                bar.set_postfix(bars=new_rows, requests=requested,
                                closed_skips=skipped_closed,
                                budget=(budget.used if budget else "unlimited"),
                                day=cursor.date().isoformat())
            else:
                print(f"{c.symbol} {cursor.date()} requests={requested} new_rows={new_rows}", flush=True)
    finally:
        if bar is not None:
            bar.close()
    return dict(symbol=c.symbol, localSymbol=getattr(c, "localSymbol", "") or c.symbol,
                conId=c.conId, status=status, requests=requested, new_rows=new_rows,
                weekly_closed_skips=skipped_closed)


async def discover_cash_contracts(ib, settings: Settings) -> tuple[list, pd.DataFrame]:
    """Explicit SMART/primaryExchange for stocks, and PHLX for index data."""
    from ib_async import Stock, Index
    contracts, rows = [], []
    specs = [
        ("TSM", "STK", Stock("TSM", "SMART", "USD", primaryExchange="NYSE")),
        ("SOXX", "STK", Stock("SOXX", "SMART", "USD", primaryExchange="NASDAQ")),
        ("SOX", "IND", Index("SOX", "PHLX", "USD")),
        ("SDL", "IND", Index("SDL", "PHLX", "USD")),
    ]
    enabled = set(settings.stocks) | set(settings.indices)
    for symbol, kind, q in specs:
        if symbol not in enabled:
            continue
        details = await asyncio.wait_for(ib.reqContractDetailsAsync(q), timeout=30)
        matches = [d.contract for d in details if d.contract.symbol == symbol
                   and d.contract.secType == kind]
        if len(matches) != 1:
            rows.append(dict(symbol=symbol, secType=kind, status=f"matches_{len(matches)}"))
            raise RuntimeError(f"Expected 1 unique {symbol} {kind} contract; got {len(matches)}")
        c = matches[0]
        contracts.append(c)
        rows.append(dict(symbol=symbol, secType=kind, status="resolved",
                         conId=int(c.conId), localSymbol=c.localSymbol, exchange=c.exchange))
        await asyncio.sleep(settings.contract_details_interval)
    return contracts, pd.DataFrame(rows)


def most_recent_equity_close_utc(now: datetime) -> datetime:
    """Most recent completed US equity extended session end (20:00 ET).

    Used for *smoke tests* to avoid launching on a weekend/unfinished session.
    US holiday calendars and early closes are intentionally not asserted here.
    """
    current_et = now.astimezone(ZoneInfo("America/New_York"))
    session_day = current_et.date()
    if current_et.hour < 20:
        session_day -= timedelta(days=1)
    while session_day.weekday() >= 5:
        session_day -= timedelta(days=1)
    return datetime.combine(session_day, datetime.min.time(),
                            ZoneInfo("America/New_York")).replace(hour=20).astimezone(UTC)


async def head_timestamp_utc(ib, contract) -> datetime:
    """Ask IBKR for actual oldest TRADES timestamp; reject missing/naive data.

    The API can return None even when reqHistoricalDataAsync returns recent bars.
    Failure should be handled by build_cash_plan, not mistaken for no bars.
    """
    head = await asyncio.wait_for(
        ib.reqHeadTimeStampAsync(contract, whatToShow="TRADES", useRTH=False, formatDate=2),
        timeout=40)
    if head is None or head == "":
        raise RuntimeError(f"IBKR did not supply oldest historical timestamp for {contract.symbol}")
    if isinstance(head, str) and head.isdigit():
        return datetime.fromtimestamp(int(head), UTC)
    return parse_utc(head)


async def build_cash_plan(ib, contracts: list, settings: Settings, now: Optional[datetime] = None,
                          head_cache_path: Optional[Path] = None) -> pd.DataFrame:
    now = now or utc_now()
    rows = []
    # Crucial: smoke tests should test a few recent 1m bars without relying on
    # optional, frequently unreliable head-timestamp queries.
    if settings.smoke_test:
        smoke_end = most_recent_equity_close_utc(now)
        smoke_start = smoke_end - timedelta(days=max(14, settings.smoke_requests_per_contract * 4))
        for c in contracts:
            print(f"{c.symbol}: smoke test recent bars, skipping optional head timestamp", flush=True)
            rows.append(dict(symbol=c.symbol, localSymbol=c.localSymbol or c.symbol,
                        conId=int(c.conId), expiry="", contract=c,
                        start_utc=smoke_start, end_utc=smoke_end,
                        eligible=smoke_start < smoke_end,
                        head_source="smoke_not_queried", head_error=""))
        return pd.DataFrame(rows)

    completed_session_end = most_recent_equity_close_utc(now)
    head_cache = {}
    if head_cache_path is not None and head_cache_path.exists():
        try:
            head_cache = json.loads(head_cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            print("WARNING: head timestamp cache unreadable; refreshing API queries", flush=True)
    for c in contracts:
        head_source, head_error = "ibkr_head_timestamp", ""
        cache_key = f"{c.symbol}_{int(c.conId)}"
        entry = head_cache.get(cache_key, {})
        try:
            cached_at = parse_utc(entry["checked_at_utc"])
            use_cache = (now - cached_at) < timedelta(days=settings.equity_head_cache_days)
            use_cache &= bool(entry.get("start_utc"))
            if settings.equity_head_required and entry.get("head_source") != "ibkr_head_timestamp":
                use_cache = False
        except (KeyError, ValueError, TypeError):
            use_cache = False
        if use_cache:
            start = parse_utc(entry["start_utc"])
            head_source = entry["head_source"]
            head_error = entry.get("head_error", "")
            print(f"{c.symbol}: using cached head lookup ({head_source})", flush=True)
        else:
            print(f"{c.symbol}: requesting earliest historical timestamp ...", flush=True)
            try:
                head_start = await head_timestamp_utc(ib, c)
                if head_start > now:
                    raise RuntimeError(f"IBKR returned future head timestamp for {c.symbol}: {head_start}")
                start = datetime.combine(head_start.date(), datetime.min.time(), tzinfo=UTC)
            except Exception as exc:
                if settings.equity_head_required:
                    raise RuntimeError(f"Head timestamp lookup failed for {c.symbol}; "
                                       "set equity_head_required=False to permit UNVERIFIED fallback") from exc
                head_source = "fallback_floor_UNVERIFIED"
                head_error = f"{type(exc).__name__}: {exc}"
                start = datetime(settings.equity_fallback_year, 1, 1, tzinfo=UTC)
                print(f"WARNING: {c.symbol} head timestamp unavailable: {head_error}", flush=True)
                print(f"WARNING: planning backwards to {start.date()} as an unverified research floor; "
                      "will stop after prolonged empty weekdays, if enabled.", flush=True)
            if head_cache_path is not None:
                head_cache[cache_key] = dict(start_utc=start.isoformat(),
                    checked_at_utc=now.isoformat(), head_source=head_source, head_error=head_error)
                atomic_json(head_cache_path, head_cache)
        rows.append(dict(symbol=c.symbol, localSymbol=c.localSymbol or c.symbol,
                         conId=int(c.conId), expiry="", contract=c, start_utc=start,
                         end_utc=completed_session_end, eligible=start < completed_session_end,
                         head_source=head_source, head_error=head_error))
    return pd.DataFrame(rows)


async def probe_indices(ib, pacer: Optional[HistoryPacer] = None) -> pd.DataFrame:
    """A single-day market-data proof, not a claim of longest historical coverage."""
    from ib_async import Index
    pacer = pacer or HistoryPacer()
    out = []
    # Use a recent completed weekday session rather than 'now', which may be Sunday.
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    probe_date = now_et.date()
    # If it is before market close, inspect previous calendar day.
    if now_et.hour < 17:
        probe_date -= timedelta(days=1)
    while probe_date.weekday() >= 5:
        probe_date -= timedelta(days=1)
    probe_end = datetime.combine(probe_date, datetime.min.time(),
                                 ZoneInfo("America/New_York")).replace(hour=17)
    for symbol in ("SOX", "SDL"):
        c = Index(symbol, "PHLX", "USD")
        try:
            ds = await ib.reqContractDetailsAsync(c)
            matches = [d.contract for d in ds if d.contract.symbol == symbol and d.contract.secType == "IND"]
            if len(matches) != 1:
                raise RuntimeError(f"Index {symbol} contract count={len(matches)}")
            c = matches[0]
            await pacer.wait()
            bars = await ib.reqHistoricalDataAsync(c, endDateTime=probe_end, durationStr="1 D",
                       barSizeSetting="1 min", whatToShow="TRADES", useRTH=True,
                       formatDate=2, keepUpToDate=False, timeout=70)
            out.append(dict(symbol=symbol, conId=c.conId, rows=len(bars or []),
                            status="bars_received" if bars else "zero_bars_or_permission_issue"))
        except Exception as exc:
            out.append(dict(symbol=symbol, rows=0, status="error", error=str(exc)))
    return pd.DataFrame(out)


def sync_tree_to_google_drive(local_root: Path, cloud_root: Path) -> dict:
    """Incrementally copy final artifacts to a Google Drive for desktop local folder.
    Never deletes remote files. Requires Drive desktop running separately.
    """
    local_root = local_root.resolve()
    cloud_root = cloud_root.resolve()
    if local_root == cloud_root or local_root in cloud_root.parents or cloud_root in local_root.parents:
        raise ValueError("Local and cloud paths must not be nested: avoid sync loops")
    if not cloud_root.exists():
        raise FileNotFoundError(f"Sync destination does not exist: {cloud_root}")
    count, bytes_copied = 0, 0
    for src in local_root.rglob("*"):
        if not src.is_file() or src.name.endswith(".tmp") or src.name.endswith(".tmp.parquet"):
            continue
        rel = src.relative_to(local_root)
        dst = cloud_root / rel
        if dst.exists() and dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime >= src.stat().st_mtime - 1:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".copying")
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()
        count += 1
        bytes_copied += src.stat().st_size
    return dict(files_copied=count, bytes_copied=bytes_copied,
                destination=str(cloud_root), cloud_upload_verified=False)


async def run_backfill(ib, settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    settings.validate()
    assert ib.isConnected(), "TWS API is not connected"
    from tqdm.auto import tqdm
    root = Path(settings.output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    plans = []
    discoveries = []
    if settings.symbols:
        futures, fut_discovery = await discover_quarterly_contracts(ib, settings)
        discoveries.append(fut_discovery)
        if futures:
            plans.append(build_download_plan(futures, settings))
    if settings.stocks or settings.indices:
        cash, cash_discovery = await discover_cash_contracts(ib, settings)
        discoveries.append(cash_discovery)
        if cash:
            plans.append(await build_cash_plan(ib, cash, settings,
                                              head_cache_path=root / "head_timestamp_cache.json"))
    if discoveries:
        pd.concat(discoveries, ignore_index=True).to_csv(root / "contract_discovery.csv", index=False)
    if not plans:
        raise RuntimeError("No contracts found for the selected symbols")
    plan_all = pd.concat(plans, ignore_index=True)
    plan, skipped_plan = filter_eligible_plan(plan_all)
    readable = plan.drop(columns=["contract"]).copy()
    readable.to_csv(root / "download_plan.csv", index=False)
    if not skipped_plan.empty:
        skipped_plan.drop(columns=["contract"]).to_csv(root / "skipped_future_start_plans.csv", index=False)
    print(f"Contracts resolved: {len(plan_all)}; actionable windows: {len(plan)}; "
          f"excluded future/invalid windows: {len(skipped_plan)}", flush=True)
    if not readable.empty:
        print(readable[["symbol", "localSymbol", "start_utc", "end_utc"]].to_string(index=False))
    if plan.empty:
        raise RuntimeError("No eligible historical backfill windows")
    pacer = HistoryPacer(settings.min_interval_seconds, settings.requests_per_10_minutes)
    budget = RequestBudget(settings.max_requests_per_run)
    eligible_plan = plan.copy()
    if settings.smoke_test or settings.refresh_only:
        # Only newest quarterly expiry for futures, plus each stock/index.
        fut_plan = eligible_plan.loc[eligible_plan.symbol.isin(settings.symbols)]
        cash_plan = eligible_plan.loc[~eligible_plan.symbol.isin(settings.symbols)]
        selected_future = fut_plan.sort_values("expiry").groupby("symbol", group_keys=False).tail(1)
        eligible_plan = pd.concat([selected_future, cash_plan], ignore_index=True)
    # Fair rotation across symbols prevents ES/NQ history from starving TSM/SOXX.
    # Within each symbol process newest quarter first.
    eligible_plan = fair_contract_order(eligible_plan)
    results = []
    symbols_count = len(eligible_plan)
    for i, (_, row) in enumerate(eligible_plan.iterrows(), 1):
        if budget.exhausted:
            print(f"Work budget exhausted ({budget.used} requests); rerun full cell to resume.")
            break
        print(f"[{i}/{symbols_count}] {row['symbol']} {row['localSymbol']}  "
              f"{row['start_utc'].date()} → {row['end_utc'].date()} (UTC)", flush=True)
        try:
            res = await backfill_one_contract(
                ib, row, root, settings, pacer, budget,
                tqdm if settings.show_progress else None)
            report = reports_for_contract(contract_folder(root, row["contract"]))
            results.append({**res, **report})
            # Keep completed result logs even if a later contract fails or budget stops.
            pd.DataFrame(results).to_csv(root / "run_summary.csv", index=False)
            if settings.google_drive_dir:
                print("Drive sync:", sync_tree_to_google_drive(root, Path(settings.google_drive_dir)))
        except Exception as exc:
            error_text = str(exc)
            append_jsonl(root / "failures.jsonl", dict(at_utc=utc_now().isoformat(),
                symbol=row["symbol"], localSymbol=row["localSymbol"],
                conId=row["conId"], error=error_text,
                checkpoint_preserved=True))
            is_systemic = (not ib.isConnected() or "pacing" in error_text.lower()
                           or "tws disconnected" in error_text.lower())
            if is_systemic or not settings.continue_on_contract_error:
                print(f"STOP on {row['localSymbol']}: {exc}. Prior checkpoint is preserved.", flush=True)
                raise
            print(f"UNRESOLVED on {row['localSymbol']}: {exc}. "
                  "Checkpoint unchanged; continuing other contracts.", flush=True)
            results.append(dict(symbol=row["symbol"], localSymbol=row["localSymbol"],
                conId=row["conId"], status="failed_retryable",
                requests=0, new_rows=0, error=error_text))
            pd.DataFrame(results).to_csv(root / "run_summary.csv", index=False)
    summary = pd.DataFrame(results)
    failures = (int((summary["status"] == "failed_retryable").sum())
                if "status" in summary else 0)
    status = ("completed_with_failures" if failures else
              "paused_budget" if budget.exhausted else "completed_selected")
    print(f"Run finished. Completed API windows: {budget.used}; "
          f"Contract errors: {failures}; Status: {status}. Root: {root}", flush=True)
    return readable, summary
