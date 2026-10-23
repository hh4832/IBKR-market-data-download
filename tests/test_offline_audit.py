"""Reproducible checks of historical partial one-day request windows."""
import json
from datetime import datetime,timezone
import pytest

from ibkr_backfill_audit import (
    AuditConfig,expected_standard_minutes,inspect_contract,scan_dataset,main
)

def test_standard_weekday_and_weekend_grid():
    assert expected_standard_minutes(datetime(2026,10,20,0,0,tzinfo=timezone.utc))==1380
    assert expected_standard_minutes(datetime(2026,10,18,0,0,tzinfo=timezone.utc))==0

def test_suspect_hmds_120_bars_flagged_without_mutating_checkpoint(tmp_path):
    folder=tmp_path/"raw"/"NQ"/"NQZ5_563947738"
    folder.mkdir(parents=True)
    record={"end_utc":"2025-10-08T00:00:00+00:00","rows":120,"status":"ok"}
    (folder/"requests.jsonl").write_text(json.dumps(record)+"\n",encoding="utf-8")
    checkpoint={"next_cursor_utc":"2025-10-07T00:00:00+00:00","coverage_mode":"roll_window"}
    original=json.dumps(checkpoint)
    (folder/"checkpoint.json").write_text(original,encoding="utf-8")
    summary,issues=inspect_contract(folder)
    assert summary["suspicious_request_windows"]==1
    assert issues[0]["observed_rows"]==120
    assert issues[0]["regular_expected_minutes"]==1380
    assert issues[0]["classification"].startswith("SUSPECT_SHORT")
    totals,flagged=scan_dataset(tmp_path)
    assert len(totals)==1 and len(flagged)==1
    assert (folder/"checkpoint.json").read_text(encoding="utf-8")==original

def test_regular_or_early_close_larger_request_not_incorrectly_flagged(tmp_path):
    folder=tmp_path/"raw"/"NQ"/"NQZ6_563947726"
    folder.mkdir(parents=True)
    (folder/"requests.jsonl").write_text(json.dumps({
        "end_utc":"2026-09-08T00:00:00+00:00","rows":1000,"status":"ok"
    })+"\n",encoding="utf-8")
    result,issues=inspect_contract(folder)
    assert result["suspicious_request_windows"]==0 and issues==[]

def test_malformed_request_log_line_is_reported(tmp_path):
    folder=tmp_path/"raw"/"NQ"/"NQZ6_563947726"
    folder.mkdir(parents=True)
    (folder/"requests.jsonl").write_text('{"bad": \n',encoding="utf-8")
    stat,issues=inspect_contract(folder)
    assert stat["malformed_jsonl_lines"]==1

def test_audit_does_not_write_inside_original_dataset(tmp_path):
    data=tmp_path/"original"
    (data/"raw").mkdir(parents=True)
    with pytest.raises(SystemExit):
        main(["--data-root",str(data),"--report-dir",str(data/"raw"/"audit")])

def test_config_fails_on_invalid_threshold():
    with pytest.raises(ValueError):
        AuditConfig(short_response_threshold=1.0).validate()
