"""Optional Jupyter status/connection monitor for IBKR Market Data Backfill v1.5.

This add-on does not alter market-data requests, rate limiting, or checkpoints.
Usage:
    from ibkr_backfill_watchdog import run_with_monitor, show_monitor_status
    full_plan, full_results = await run_with_monitor(ib, cfg, run_backfill)
"""
from __future__ import annotations

import asyncio
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _atomic_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temp, path)


def _chime() -> None:
    # Only a non-fatal convenience on Windows; never blocks a market-data run.
    try:
        import winsound
        winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass


class BackfillWatchdog:
    def __init__(self, ib: Any, root: str | Path, interval: int = 20) -> None:
        self.ib = ib
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.status_file = self.root / "backfill_monitor_status.json"
        self.log_file = self.root / "backfill_monitor_events.jsonl"
        self.interval = interval
        self.started = _now()
        self.finished: datetime | None = None
        self.state = "RUNNING"
        self.issues: set[str] = set()
        self.last_event = "開始監控"
        self.last_error = ""
        self.alert_count = 0
        self._display_handle = None
        self._subscribed = False
        self._task = None

    def _record(self, event: str, message: str, alarm: bool = False) -> None:
        timestamp = _now().isoformat()
        self.last_event = f"{timestamp} | {event}: {message}"
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at_utc": timestamp, "event": event,
                                "message": message}, ensure_ascii=False) + "\n")
        print(f"\n[IBKR MONITOR] {event}: {message}", flush=True)
        if alarm:
            self.alert_count += 1
            _chime()
        self._publish()

    def _on_disconnect(self, *args: Any) -> None:
        if "TWS_API" not in self.issues:
            self.issues.add("TWS_API")
            self._record("DISCONNECTED", "Python 與 TWS 的 API Socket 中斷", alarm=True)

    def _on_connect(self, *args: Any) -> None:
        if "TWS_API" in self.issues:
            self.issues.remove("TWS_API")
            self._record("RECOVERED", "Python 與 TWS 的 API Socket 恢復")

    def _on_error(self, req_id: int, error_code: int,
                  error_string: str, contract: Any = None) -> None:
        # These messages are from TWS: the local socket can still be connected.
        code = int(error_code)
        if code in {1100, 2110}:
            if "IBKR_SERVER" not in self.issues:
                self.issues.add("IBKR_SERVER")
                self._record("SERVER_LOST", f"IBKR {code}: {error_string}", alarm=True)
        elif code == 1300:
            if "TWS_API" not in self.issues:
                self.issues.add("TWS_API")
                self._record("SOCKET_RESET", f"IBKR {code}: {error_string}", alarm=True)
        elif code == 2105:
            if "HISTORY_FARM" not in self.issues:
                self.issues.add("HISTORY_FARM")
                self._record("HISTORY_FARM_LOST", f"IBKR {code}: {error_string}", alarm=True)
        elif code in {1101, 1102}:
            if "IBKR_SERVER" in self.issues:
                self.issues.remove("IBKR_SERVER")
                self._record("SERVER_RECOVERED", f"IBKR {code}: {error_string}")
        elif code == 2106:
            if "HISTORY_FARM" in self.issues:
                self.issues.remove("HISTORY_FARM")
                self._record("HISTORY_FARM_RECOVERED", f"IBKR {code}: {error_string}")
        # 2107/2108 = normal idle, not a broken connection.

    def _current_state(self) -> str:
        if self.finished is not None:
            return self.state
        if not self.ib.isConnected() or self.issues:
            return "CONNECTION_WARNING"
        return "RUNNING"

    def _publish(self) -> None:
        state = self._current_state()
        now = _now()
        obj = {
            "state": state,
            "heartbeat_utc": now.isoformat(),
            "heartbeat_taipei": now.astimezone(ZoneInfo("Asia/Taipei")).isoformat(),
            "started_utc": self.started.isoformat(),
            "finished_utc": self.finished.isoformat() if self.finished else None,
            "api_socket_connected": bool(self.ib.isConnected()),
            "active_connection_issues": sorted(self.issues),
            "alert_count": self.alert_count,
            "last_event": self.last_event,
            "last_error": self.last_error,
            "data_root": str(self.root),
        }
        _atomic_json(self.status_file, obj)
        if self._display_handle is not None:
            color = "#137333" if state == "RUNNING" else (
                "#b3261e" if state in {"CONNECTION_WARNING", "FAILED", "INTERRUPTED", "FINISHED_WITH_FAILURES"}
                else "#915400")
            age_min = int((_now() - self.started).total_seconds() // 60)
            detail = (f"API connected: {obj['api_socket_connected']} | "
                      f"IBKR warning: {', '.join(sorted(self.issues)) or 'none'} | "
                      f"elapsed: {age_min} min | alerts: {self.alert_count}")
            message = (f"<div style='border:2px solid {color};border-radius:10px;"
                       f"padding:12px;margin:5px 0'>"
                       f"<b style='font-size:21px;color:{color}'>IBKR BACKFILL: {html.escape(state)}</b><br>"
                       f"<span>{html.escape(detail)}</span><br>"
                       f"<small>Heartbeat (Taipei): {html.escape(obj['heartbeat_taipei'])}</small><br>"
                       f"<small>Latest event: {html.escape(self.last_event)}</small></div>")
            try:
                from IPython.display import HTML
                self._display_handle.update(HTML(message))
            except Exception:
                pass

    async def _heartbeat(self) -> None:
        while True:
            if not self.ib.isConnected():
                self._on_disconnect()
            self._publish()
            await asyncio.sleep(self.interval)

    def _subscribe(self) -> None:
        self.ib.disconnectedEvent += self._on_disconnect
        self.ib.connectedEvent += self._on_connect
        self.ib.errorEvent += self._on_error
        self._subscribed = True

    def _unsubscribe(self) -> None:
        if not self._subscribed:
            return
        self.ib.disconnectedEvent -= self._on_disconnect
        self.ib.connectedEvent -= self._on_connect
        self.ib.errorEvent -= self._on_error
        self._subscribed = False

    async def run(self, runner: Any) -> Any:
        if not self.ib.isConnected():
            raise ConnectionError("TWS API 尚未連線；請先重新連線。")
        try:
            from IPython.display import HTML, display
            self._display_handle = display(HTML("<b>啟動 IBKR 連線監控...</b>"), display_id=True)
        except Exception:
            self._display_handle = None
        self._subscribe()
        self._publish()
        self._task = asyncio.create_task(self._heartbeat())
        try:
            result = await runner()
            # A successfully returned run may still contain per-contract failures.
            failed_contracts = 0
            try:
                _, results = result
                failed_contracts = int(results["status"].eq("failed_retryable").sum())
            except (KeyError, TypeError, ValueError, AttributeError):
                pass
            self.state = "FINISHED_WITH_FAILURES" if failed_contracts or self.issues else "FINISHED_REVIEW_COVERAGE"
            self._record("RUN_RETURNED", f"回補程序已結束；未解決合約 {failed_contracts} 張。請檢查 coverage / checkpoint。")
            return result
        except BaseException as exc:
            self.state = "INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "FAILED"
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._record("RUN_STOPPED", self.last_error, alarm=True)
            raise
        finally:
            self.finished = _now()
            self._publish()
            if self._task is not None:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            self._unsubscribe()


async def run_with_monitor(ib: Any, cfg: Any, run_backfill: Any) -> Any:
    """Call with existing connected ib, v1.5 cfg, and imported run_backfill."""
    return await BackfillWatchdog(ib, cfg.output_dir).run(lambda: run_backfill(ib, cfg))


def show_monitor_status(root: str | Path, stale_after_seconds: int = 300) -> dict[str, Any] | None:
    """Read persisted heartbeat. An old RUNNING heartbeat indicates possible lost kernel."""
    file = Path(root).expanduser().resolve() / "backfill_monitor_status.json"
    if not file.exists():
        print("找不到監控檔案，請先執行監控回補。")
        return None
    data = json.loads(file.read_text(encoding="utf-8"))
    last = datetime.fromisoformat(data["heartbeat_utc"])
    age = int((_now() - last).total_seconds())
    if data["state"] in {"RUNNING", "CONNECTION_WARNING"} and age > stale_after_seconds:
        print(f"[WARNING] 心跳已停止 {age} 秒。可能是 Kernel、TWS 或電腦中斷，請勿假設仍在下載。")
    print("state:", data["state"], "| heartbeat age:", age, "seconds")
    print("alert_count:", data["alert_count"], "| active issues:", data["active_connection_issues"])
    print("last event:", data["last_event"])
    if data.get("last_error"):
        print("last error:", data["last_error"])
    return data
