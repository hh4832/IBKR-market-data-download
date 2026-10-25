import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from ibkr_gap_evidence import assess_forward_ticks
from verified_backup import create_backup, verify_archive


class EvidenceTests(unittest.TestCase):
    def assess(self, ticks, **kwargs):
        return assess_forward_ticks('2025-01-01T23:00Z', '2025-01-01T23:02Z', ticks, completed=kwargs.pop('completed', True), **kwargs)

    def tick(self, time='2025-01-01T23:02:07Z'):
        return dict(time=time, price=100, size=1)

    def test_provider_scope_only(self):
        result = self.assess([self.tick()])
        self.assertEqual(result['status'], 'EXPLAINED_IB_FEED_NO_REPORTED_TRADES')
        self.assertFalse(result['exchange_wide_no_trade_proven'])

    def test_empty_error_incomplete_never_resolves(self):
        for ticks, kw in [([], {}), ([self.tick()], dict(errors=[162])), ([self.tick()], dict(completed=False))]:
            self.assertEqual(self.assess(ticks, **kw)['status'], 'UNRESOLVED')

    def test_tick_inside_gap_requires_review(self):
        self.assertEqual(self.assess([self.tick('2025-01-01T23:01:59Z')])['status'], 'TICKS_EXIST_BAR_GAP_REQUIRES_REVIEW')

    def test_reverse_or_naive_rejected(self):
        for ticks in [[self.tick('2025-01-01T22:59Z')], [self.tick('2025-01-01T23:02:07')], [self.tick('2025-01-01T23:04Z'), self.tick()]]:
            with self.assertRaises(ValueError):
                self.assess(ticks)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'source'
        self.source.mkdir()
        (self.source/'bars.txt').write_bytes(b'original bytes')
        self.dest = self.root/'backup.zip'

    def test_default_preview_writes_nothing(self):
        r = create_backup([('dataset', self.source)], self.dest)
        self.assertEqual(r['files'], 1)
        self.assertFalse(self.dest.exists())

    def test_copy_verifies_content_and_preserves_source(self):
        before = (self.source/'bars.txt').stat().st_mtime_ns
        r = create_backup([('dataset', self.source)], self.dest, execute=True)
        self.assertEqual(r['status'], 'VERIFIED_SOURCE_AND_ARCHIVE_IDENTICAL_SHA256')
        self.assertEqual(verify_archive(self.dest)[0]['sha256'], hashlib.sha256(b'original bytes').hexdigest())
        self.assertEqual((self.source/'bars.txt').stat().st_mtime_ns, before)
        with self.assertRaises(FileExistsError):
            create_backup([('dataset', self.source)], self.dest, execute=True)

    def test_destination_inside_source_rejected(self):
        with self.assertRaises(ValueError):
            create_backup([('dataset', self.source)], self.source/'backup.zip')

    def test_overlapping_sources_and_duplicate_labels_rejected(self):
        sub = self.source/'sub'; sub.mkdir()
        for sources in [[('a', self.source), ('b', sub)], [('A', self.source), ('a', sub)]]:
            with self.assertRaises(ValueError):
                create_backup(sources, self.dest)

    def test_tampered_archive_rejected(self):
        with zipfile.ZipFile(self.dest, 'x') as z:
            z.writestr('dataset/bars.txt', 'tampered')
            z.writestr('BACKUP_MANIFEST.json', json.dumps([dict(path='dataset/bars.txt', bytes=14, sha256='bad')]))
        with self.assertRaises(ValueError):
            verify_archive(self.dest)

    def test_source_mutation_rejected(self):
        real_open = zipfile.ZipFile.open
        def mutate(z, name, mode='r', **kw):
            if mode == 'w' and name == 'dataset/bars.txt':
                (self.source/'bars.txt').write_bytes(b'changed length')
            return real_open(z, name, mode, **kw)
        with patch.object(zipfile.ZipFile, 'open', mutate):
            with self.assertRaises(RuntimeError):
                create_backup([('dataset', self.source)], self.dest, execute=True)


if __name__ == '__main__':
    unittest.main()
