import json
from pathlib import Path
import pytest
from copy_existing_history import (
    SnapshotError, preview, copy_snapshot, verify_snapshot, main
)


@pytest.fixture
def dataset(tmp_path):
    source = tmp_path / "old" / "IBKR_MarketData"
    source.mkdir(parents=True)
    (source/"raw"/"NQ"/"NQZ6_123"/"chunks").mkdir(parents=True)
    (source/"raw"/"ES"/"ESZ6_456"/"chunks").mkdir(parents=True)
    (source/"raw"/"TSM"/"TSM_789"/"chunks").mkdir(parents=True)
    (source/"raw"/"SOXX"/"SOXX_123"/"chunks").mkdir(parents=True)
    (source/"raw"/"NQ"/"NQZ6_123"/"chunks"/"end_1.parquet").write_bytes(b"\0PARET\0")
    (source/"raw"/"NQ"/"NQZ6_123"/"consolidated.parquet").write_bytes(b"parquet")
    (source/"raw"/"NQ"/"NQZ6_123"/"checkpoint.json").write_text(
        json.dumps({"next_cursor_utc":"2026-10-16T22:00:00Z"}))
    (source/"raw"/"NQ"/"NQZ6_123"/"requests.jsonl").write_text('{"rows": 60}\n')
    (source/"raw"/"NQ"/"NQZ6_123"/"reports").mkdir()
    (source/"raw"/"NQ"/"NQZ6_123"/"reports"/"quality.jsonl").write_text('{"ok": true}\n')
    (source/"head_timestamp_cache.json").write_text("{}")
    (source/"download_plan.csv").write_text("contract,end\n")
    (source/"run_summary.csv").write_text("status\n")
    (source/"contract_discovery.csv").write_text("localSymbol\n")
    (source/"backfill_monitor_status.json").write_text("{}")
    target = tmp_path / "new" / "IBKR_MarketData"
    target.parent.mkdir()
    return source, target


def test_preview_is_readonly_and_counts_all_metadata(dataset):
    source, dest = dataset
    a = preview(source,dest)
    assert a["status"] == "PREVIEW_ONLY_NO_FILES_COPIED"
    assert a["files"] == 10
    assert not dest.exists()
    assert (source/"raw"/"NQ"/"NQZ6_123"/"checkpoint.json").exists()


def test_snapshot_copies_every_file_empty_directory_and_sha(dataset):
    source,dest=dataset
    result=copy_snapshot(source,dest)
    assert result["status"] == "COPIED_AND_SHA256_VERIFIED_SOURCE_PRESERVED"
    assert result["files"] == 10
    assert (dest/"raw"/"ES"/"ESZ6_456"/"chunks").is_dir()
    assert (dest/"raw"/"NQ"/"NQZ6_123"/"checkpoint.json").read_bytes() == (source/"raw"/"NQ"/"NQZ6_123"/"checkpoint.json").read_bytes()
    assert verify_snapshot(source,dest)["files"] == 10
    assert not list(dest.parent.glob(".*.ibkr-stage-*"))


def test_reject_destination_existing_even_empty(dataset):
    src,dest=dataset
    dest.mkdir()
    with pytest.raises(SnapshotError, match="already exists"):
        copy_snapshot(src,dest)


@pytest.mark.parametrize("nested",["child","ancestor","same"])
def test_reject_overlapping_source_and_target(dataset,nested):
    src,dest=dataset
    target = src/"backup" if nested=="child" else src.parent if nested=="ancestor" else src
    with pytest.raises(SnapshotError,match="separate, non-nested"):
        preview(src,target)


def test_source_must_contain_raw_and_data(dataset):
    src,dest=dataset
    (src/"raw").rename(src/"renamed")
    with pytest.raises(SnapshotError,match="raw/"):
        preview(src,dest)


def test_verify_detects_corruption_without_repair(dataset):
    src,dest=dataset
    copy_snapshot(src,dest)
    (dest/"raw"/"NQ"/"NQZ6_123"/"checkpoint.json").write_text("tampered")
    with pytest.raises(SnapshotError,match="mismatch"):
        verify_snapshot(src,dest)
    assert "next_cursor_utc" in (src/"raw"/"NQ"/"NQZ6_123"/"checkpoint.json").read_text()


def test_verify_detects_same_size_tamper(dataset):
    src,dest=dataset
    copy_snapshot(src,dest)
    file=dest/"raw"/"NQ"/"NQZ6_123"/"consolidated.parquet"
    file.write_bytes(b"PARQUET")
    with pytest.raises(SnapshotError,match="SHA256 mismatch"):
        verify_snapshot(src,dest)


def test_source_symlink_is_rejected(dataset):
    src,dest=dataset
    outside=src.parent/"elsewhere.txt"
    outside.write_text("don't follow me")
    try:
        (src/"link").symlink_to(outside)
    except (OSError,NotImplementedError):
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(SnapshotError,match="Symlink"):
        preview(src,dest)


def test_verify_only_requires_destination(dataset):
    src,dest=dataset
    with pytest.raises(SnapshotError,match="Destination not found"):
        verify_snapshot(src,dest)


def test_cli_preview_writes_no_data(dataset,capsys):
    src,dest=dataset
    assert main(["--source",str(src),"--dest",str(dest)])==0
    assert "PREVIEW_ONLY" in capsys.readouterr().out
    assert not dest.exists()
