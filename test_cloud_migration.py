import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('cloud_restore', Path(__file__).parent / 'scripts/restore_cloud_migration.py')
restore_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore_module)


class MigrationTests(unittest.TestCase):
    def test_credentials_paths_links_and_traversal_are_not_allowed(self):
        for name in ('../service_requests.db', '/tmp/service_requests.db', '.env',
                     'chrome_robot_profile_v4/Cookies', 'vision_debug/../../.env',
                     'vision_debug\\evil.json', 'vision_debug/secret.py', 'dashboard_access.db'):
            self.assertFalse(restore_module.allowed(name), name)
        for name in ('service_requests.db', 'vision_debug/registration.jpg',
                     'offering/intercars_output/requests/request_37_products.json'):
            self.assertTrue(restore_module.allowed(name), name)

    def test_existing_destination_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as work:
            target = Path(work)
            (target / 'important').write_text('original')
            with self.assertRaises(ValueError):
                restore_module.restore(target / 'unused.tar.gz', '', target)
            self.assertEqual((target / 'important').read_text(), 'original')

    def test_bad_hash_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / 'invalid.tar.gz'
            source.write_bytes(b'invalid')
            target = Path(work) / 'new-data'
            with self.assertRaises(ValueError):
                restore_module.restore(source, '0' * 64, target)
            self.assertFalse(target.exists())

    def test_unlisted_member_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / 'invalid.tar.gz'
            manifest = {'format': 'albatros-cloud-migration-v1', 'contains_credentials': False,
                        'files': {}, 'counts': {}}
            with tarfile.open(source, 'w:gz') as archive:
                for name, content in [('manifest.json', json.dumps(manifest).encode()),
                                      ('request_status.json', b'{}')]:
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
            target = Path(work) / 'new-data'
            with self.assertRaises(ValueError):
                restore_module.restore(source, hashlib.sha256(source.read_bytes()).hexdigest(), target)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
