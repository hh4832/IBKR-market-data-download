"""Safely copy a complete existing IBKR_MarketData dataset to a new location.

Pure standard library: no TWS, API requests, package installation or Drive sync.
Default mode is PREVIEW ONLY. Use --execute to create a never-before-existing
destination directory, and --verify-only to check an already copied destination.
Preserves every regular file and directory, including checkpoints and logs.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
import uuid


BUFFER_SIZE = 1024 * 1024


class SnapshotError(RuntimeError):
    """Refuse potentially destructive or unverifiable copy operations."""


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _reject_reparse(path: Path) -> None:
    """Refuse symlinks and Windows reparse/junction objects (incl. directory)."""
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SnapshotError(f"Missing source item: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise SnapshotError(f"Symlink / junction / reparse point not supported: {path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise SnapshotError(f"Special file not supported: {path}")


def _sha256(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        for block in iter(lambda: handle.read(BUFFER_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(root: Path) -> tuple[list[Path], list[tuple[Path, int]]]:
    """Inventory all items under root; no symlink/junction traversal."""
    _reject_reparse(root)
    dirs: list[Path] = [Path(".")]
    files: list[tuple[Path, int]] = []
    for base, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        _reject_reparse(base_path)
        for name in sorted(dirnames):
            child = base_path / name
            _reject_reparse(child)
            dirs.append(child.relative_to(root))
        for name in sorted(filenames):
            child = base_path / name
            _reject_reparse(child)
            metadata = child.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise SnapshotError(f"Not a regular file: {child}")
            files.append((child.relative_to(root), metadata.st_size))
    dirs.sort(key=lambda x: str(x))
    files.sort(key=lambda x: str(x[0]))
    return dirs, files


def _validate(
    source: str | Path, destination: str | Path, *, check_destination_absent: bool
) -> tuple[Path, Path, list[Path], list[tuple[Path, int]]]:
    src = _resolve(source)
    dst = _resolve(destination)
    if not src.is_dir() or not (src / "raw").is_dir():
        raise SnapshotError(f"Expected IBKR_MarketData dataset with raw/: {src}")
    if src == dst or src in dst.parents or dst in src.parents:
        raise SnapshotError("Source and destination must be separate, non-nested paths")
    if src == Path(src.anchor) or dst == Path(dst.anchor):
        raise SnapshotError("Drive roots cannot be source/destination")
    if not dst.parent.is_dir():
        raise SnapshotError(f"Destination parent directory must already exist: {dst.parent}")
    # Reject a non-existing destination reached via junction/symlink parent by
    # resolving the ancestor above. The source itself may not contain reparse
    # points; its parent may be a legitimate synced/Windows path.
    if check_destination_absent and os.path.lexists(dst):
        raise SnapshotError(f"Destination already exists; refuse to merge/overwrite: {dst}")
    dirs, files = _inventory(src)
    if not files:
        raise SnapshotError(f"Dataset is empty: {src}")
    return src, dst, dirs, files


def preview(source: str | Path, destination: str | Path) -> dict:
    """Read-only inventory. Computes no file hashes and writes nothing."""
    src, dst, dirs, files = _validate(source, destination, check_destination_absent=True)
    return {
        "source": str(src), "destination": str(dst),
        "directories": len(dirs), "files": len(files),
        "total_bytes": sum(size for _, size in files),
        "status": "PREVIEW_ONLY_NO_FILES_COPIED",
    }


def verify_snapshot(source: str | Path, destination: str | Path) -> dict:
    """Read-only exact relative paths + bytes + per-file SHA256 comparison."""
    src, dst, src_dirs, src_files = _validate(
        source, destination, check_destination_absent=False
    )
    if not dst.is_dir():
        raise SnapshotError(f"Destination not found: {dst}")
    dst_dirs, dst_files = _inventory(dst)
    if set(src_dirs) != set(dst_dirs):
        raise SnapshotError("Directory sets differ")
    src_sizes = dict(src_files)
    dst_sizes = dict(dst_files)
    if src_sizes != dst_sizes:
        missing = set(src_sizes) - set(dst_sizes)
        extra = set(dst_sizes) - set(src_sizes)
        changed = {
            str(name) for name in set(src_sizes) & set(dst_sizes)
            if src_sizes[name] != dst_sizes[name]
        }
        raise SnapshotError(
            f"File set / size mismatch: missing={len(missing)}, extra={len(extra)}, "
            f"different_size={len(changed)}"
        )
    for name, _ in src_files:
        if _sha256(src / name) != _sha256(dst / name):
            raise SnapshotError(f"SHA256 mismatch: {name}")
    return {
        "source": str(src), "destination": str(dst), "files": len(src_files),
        "directories": len(src_dirs), "total_bytes": sum(x[1] for x in src_files),
        "status": "VERIFIED_SOURCE_AND_DESTINATION_IDENTICAL_SHA256",
    }


def copy_snapshot(source: str | Path, destination: str |Path) -> dict:
    """Copy via a newly-created sibling staging directory; publish after hashing.

    Source is never modified. Destination must NOT exist, including empty dirs.
    On failure the unpublished staging copy is removed, not original history.
    """
    src, dst, dirs, files = _validate(source, destination, check_destination_absent=True)
    stage = dst.with_name(f".{dst.name}.ibkr-stage-{uuid.uuid4().hex}")
    if os.path.lexists(stage):
        raise SnapshotError(f"Unexpected staging collision: {stage}")
    stage.mkdir(mode=0o700)
    try:
        for name in dirs:
            (stage / name).mkdir(parents=True, exist_ok=True)
        # Hash source just before copying; rehash source and stage afterward.
        recorded: dict[Path, str] = {}
        for name, size in files:
            source_file = src / name
            target_file = stage / name
            before = _sha256(source_file)
            if source_file.stat().st_size != size:
                raise SnapshotError(f"Source changed size before copying: {name}")
            # Destination path is known to be unique under new staging dir.
            with source_file.open("rb") as in_handle, target_file.open("xb") as out_handle:
                shutil.copyfileobj(in_handle, out_handle, BUFFER_SIZE)
                out_handle.flush()
                os.fsync(out_handle.fileno())
            shutil.copystat(source_file, target_file)
            recorded[name] = before
            if target_file.stat().st_size != size or _sha256(target_file) != before:
                raise SnapshotError(f"Stage verification failed: {name}")
        # Guard against source mutation, missing files, newly written files or
        # new directories during the potentially long copy operation.
        final_dirs, final_files = _inventory(src)
        if dirs != final_dirs or files != final_files:
            raise SnapshotError("Source inventory changed while copying; retry later")
        for name in recorded:
            if _sha256(src / name) != recorded[name]:
                raise SnapshotError(f"Source content changed while copying: {name}")
        # The copy remains private until all checks pass. Do not overwrite.
        if os.path.lexists(dst):
            raise SnapshotError(f"Destination appeared during copying; refusing overwrite: {dst}")
        os.rename(stage, dst)
        return {
            "source": str(src), "destination": str(dst), "files": len(files),
            "directories": len(dirs), "total_bytes": sum(size for _, size in files),
            "status": "COPIED_AND_SHA256_VERIFIED_SOURCE_PRESERVED",
        }
    except Exception:
        # Clean only our newly allocated stage, never original or prior destination.
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview/copy/verify IBKR_MarketData to an independent folder (no IBKR API)"
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Create brand-new target and copy")
    mode.add_argument("--verify-only", action="store_true", help="Check completed target, no writes")
    args = parser.parse_args(argv)
    try:
        if args.execute:
            result = copy_snapshot(args.source, args.dest)
        elif args.verify_only:
            result = verify_snapshot(args.source, args.dest)
        else:
            result = preview(args.source, args.dest)
    except (SnapshotError, OSError, ValueError) as exc:
        parser.exit(2, f"REFUSED / FAILED: {exc}\n")
    for k, v in result.items():
        print(f"{k}: {v}")
    if not args.execute and not args.verify_only:
        print("Only a preview. Add --execute to perform a complete verified copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
