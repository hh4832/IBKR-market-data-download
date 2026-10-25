"""Read-only preview or verified ZIP backup of explicitly selected local folders.

Standard library only; never imports a downloader or connects to a service.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


def is_link(path):
    # Python 3.11 has no Path.is_junction(); Windows reparse points include junctions.
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def sha_stream(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(block)
    return h.hexdigest()


def collect(sources, destination):
    destination = Path(destination).resolve()
    files = []
    labels = set()
    roots = []
    for label, source in sources:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', label) or label.casefold() in labels:
            raise ValueError('Labels must be unique simple names')
        labels.add(label.casefold())
        root = Path(source)
        if is_link(root):
            raise ValueError('Linked roots are not supported')
        root = root.resolve()
        if not root.is_dir() or destination == root or root in destination.parents:
            raise ValueError('Source must be a folder; destination must be outside it')
        if any(root == other or root in other.parents or other in root.parents for other in roots):
            raise ValueError('Source folders must not overlap')
        roots.append(root)
        for p in sorted(root.rglob('*')):
            if is_link(p):
                raise ValueError(f'Linked input is not supported: {p}')
            if p.is_file():
                st = p.stat()
                files.append((p, label + '/' + p.relative_to(root).as_posix(), st.st_size, st.st_mtime_ns))
    if not files:
        raise ValueError('No files selected')
    return files


def verify_archive(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive members')
        manifest = json.loads(z.read('BACKUP_MANIFEST.json'))
        if set(names) != {'BACKUP_MANIFEST.json'} | {r['path'] for r in manifest}:
            raise ValueError('Archive inventory differs from manifest')
        for r in manifest:
            with z.open(r['path']) as f:
                if sha_stream(f) != r['sha256'] or z.getinfo(r['path']).file_size != r['bytes']:
                    raise ValueError(f'Archive member differs: {r["path"]}')
    return manifest


def create_backup(sources, destination, *, execute=False):
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('Destination must not already exist')
    files = collect(sources, destination)
    preview = dict(files=len(files), total_bytes=sum(r[2] for r in files),
                   destination=str(destination), status='PREVIEW_ONLY_NO_FILES_COPIED')
    if not execute:
        return preview
    if not destination.parent.is_dir():
        raise ValueError('Destination parent must already exist')
    # Exclusive creation prevents overwriting any existing backup. On failure the
    # incomplete new archive remains without a success receipt; sources are untouched.
    manifest = []
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p, name, size, mtime in files:
            h = hashlib.sha256()
            with p.open('rb') as src, z.open(name, 'w', force_zip64=True) as dest:
                for chunk in iter(lambda: src.read(1024 * 1024), b''):
                    h.update(chunk)
                    dest.write(chunk)
            if (p.stat().st_size, p.stat().st_mtime_ns) != (size, mtime):
                raise RuntimeError(f'Input changed while reading: {p}')
            manifest.append(dict(path=name, bytes=size, sha256=h.hexdigest()))
        z.writestr('BACKUP_MANIFEST.json', json.dumps(manifest, indent=2))
    verify_archive(destination)
    # Re-hash every original to confirm it still matches the copied content.
    for (p, _, size, mtime), record in zip(files, manifest):
        with p.open('rb') as src:
            if sha_stream(src) != record['sha256']:
                raise RuntimeError(f'Source changed after backup: {p}')
        if (p.stat().st_size, p.stat().st_mtime_ns) != (size, mtime):
            raise RuntimeError(f'Source metadata changed after backup: {p}')
    with destination.open('rb') as f:
        digest = sha_stream(f)
    return dict(preview, status='VERIFIED_SOURCE_AND_ARCHIVE_IDENTICAL_SHA256',
                archive_bytes=destination.stat().st_size, archive_sha256=digest)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', nargs=2, action='append', metavar=('LABEL', 'FOLDER'))
    p.add_argument('--destination', type=Path)
    p.add_argument('--execute', action='store_true')
    p.add_argument('--verify', type=Path)
    args = p.parse_args()
    if args.verify:
        if args.source or args.destination or args.execute:
            p.error('--verify must be used alone')
        print(json.dumps(dict(status='ARCHIVE_MEMBERS_MATCH_EMBEDDED_MANIFEST', files=len(verify_archive(args.verify)))))
        return
    if not args.source or not args.destination:
        p.error('--source and --destination are required')
    print(json.dumps(create_backup(args.source, args.destination, execute=args.execute), indent=2))


if __name__ == '__main__':
    main()
