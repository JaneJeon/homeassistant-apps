import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import unittest
from unittest.mock import patch

os.environ['VICTORIA_ENGINE'] = 'victorialogs'
SPEC = importlib.util.spec_from_file_location('native_backup', Path(__file__).resolve().parents[1] / 'native_backup.py')
m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)

START_SPEC = importlib.util.spec_from_file_location('start', Path(__file__).resolve().parents[1] / 'start.py')
start = importlib.util.module_from_spec(START_SPEC); START_SPEC.loader.exec_module(start)

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        for key, name in [('DATA',''),('LIVE','live'),('WORK','work'),('BACKUP','backup'),('INSTALL','installation.json'),('READY','live-ready.json'),('JOURNAL','operation.json')]: setattr(m, key, root / name)
        m.startup('init-empty')
        (m.LIVE / 'partitions/20261030/snapshots/test').mkdir(parents=True)
        self.snapshot = m.LIVE / 'partitions/20261030/snapshots/test'
        (self.snapshot / 'data').write_text('test logs')
        for kind in ('datadb', 'indexdb'):
            (self.snapshot / kind).mkdir()
            (self.snapshot / kind / 'parts.json').write_text('[]')
    def tearDown(self): self.tmp.cleanup()
    def do_export(self):
        def api(path, params=None): return [str(self.snapshot)] if path.endswith('/create') else {}
        with patch.object(m, 'api', side_effect=api): m.export()
    def test_export_restore_and_corruption(self):
        self.do_export(); manifest = m.validate_backup()
        self.assertEqual(manifest['partitions'], ['20261030'])
        self.assertEqual((m.BACKUP / 'payload/partitions/20261030/data').read_text(), 'test logs')
        shutil.rmtree(m.LIVE); m.startup('restore_only')
        self.assertEqual((m.LIVE / 'partitions/20261030/data').read_text(), 'test logs')
        shutil.rmtree(m.LIVE); (m.BACKUP / 'payload/partitions/20261030/data').write_text('bad')
        with self.assertRaises(ValueError): m.startup('restore_only')
        self.assertFalse(m.LIVE.exists())
    def test_failed_export_keeps_previous_and_pending_fails_closed(self):
        self.do_export(); previous = (m.BACKUP / 'manifest.json').read_bytes()
        with patch.object(m, 'api', side_effect=RuntimeError('snapshot failure')):
            with self.assertRaises(RuntimeError): m.export()
        self.assertEqual(previous, (m.BACKUP / 'manifest.json').read_bytes())
        m.durable_json(m.JOURNAL, {'operation':'publish'})
        with self.assertRaises(ValueError): m.startup('restore_only')
    def test_missing_identity_or_uncommitted_live_never_initializes(self):
        m.INSTALL.unlink()
        with self.assertRaises(ValueError): m.startup('import_legacy')
        self.assertTrue(m.LIVE.exists())
    def test_symlinks_and_unlisted_payload_rejected(self):
        self.do_export(); (m.BACKUP / 'payload/extra').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError): m.validate_backup()
    def test_cleanup_targets_only_owned_snapshot(self):
        calls = []
        def api(path, params=None):
            calls.append((path, params))
            if path.endswith('/create'): return [str(self.snapshot)]
            raise RuntimeError('injected cleanup failure')
        with patch.object(m, 'api', side_effect=api): m.export()
        self.assertEqual(calls[-1], ('/internal/partition/snapshot/delete', {'path': str(self.snapshot)}))
        self.assertFalse(m.JOURNAL.exists())
        m.validate_backup()

    def test_manifest_failures_and_interrupted_publication(self):
        self.do_export()
        original = (m.BACKUP / 'manifest.json').read_text()
        for change in ('identity', 'timestamp', 'generation', 'missing'):
            with self.subTest(change=change):
                (m.BACKUP / 'manifest.json').write_text(original)
                if change == 'missing': (m.BACKUP / 'manifest.json').unlink()
                else:
                    manifest = json.loads(original)
                    if change == 'identity': manifest['installation_id'] = '00000000-0000-0000-0000-000000000000'
                    if change == 'timestamp': manifest['snapshot_completed_at'] = 'not-a-timestamp'
                    if change == 'generation': manifest['generation'] = 'invalid'
                    (m.BACKUP / 'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaises((ValueError, FileNotFoundError)): m.validate_backup()
        (m.BACKUP / 'manifest.json').write_text(original)
        rename = m.os.rename
        def fail_promotion(src, dst):
            if Path(src) == m.WORK / 'export': raise OSError('injected promotion failure')
            return rename(src, dst)
        with patch.object(m.os, 'rename', side_effect=fail_promotion):
            with self.assertRaises(OSError): self.do_export()
        self.assertTrue(m.JOURNAL.exists())
        with self.assertRaises(ValueError): m.startup('restore_only')

    def test_missing_native_part_fails(self):
        (self.snapshot / 'datadb' / 'parts.json').write_text('["ABCDEF1234567890"]')
        with self.assertRaises(ValueError): self.do_export()
        self.assertFalse(m.BACKUP.exists())

    def test_managed_flags_rejected_in_both_spellings(self):
        for arg in ('-storageDataPath=/tmp/x', '--storageDataPath=/tmp/x', '--httpListenAddr', '-partitionManageAuthKey=key'):
            with self.subTest(arg=arg), self.assertRaises(ValueError): start.validate_args([arg])
        start.validate_args(['-search.maxQueryDuration=30s'])

    def test_supervisor_directory_excludes(self):
        # Supervisor matches each full path with PurePath.match before descending.
        for name in ('live', 'work'):
            self.assertTrue(PurePosixPath('/usr/share/hassio/addons/data/example', name).match(name + '/'))
        self.assertFalse(PurePosixPath('/usr/share/hassio/addons/data/example/backup').match('work/'))

if __name__ == '__main__': unittest.main()
