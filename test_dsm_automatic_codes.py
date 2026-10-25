import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dsm_automatic_codes import lookup, package_text, vehicles

V1 = 'WBA00000000000004'
V2 = 'WVW00000000000005'


class AutomaticCodesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = [Path(self.tmp.name)/n for n in ('revision.db', 'brakes.db')]
        for path, table in zip(self.paths, ('packages', 'jobs')):
            with sqlite3.connect(path) as db:
                db.executescript(f'CREATE TABLE vehicle_vins(registration TEXT,vin TEXT); CREATE TABLE {table}(id TEXT,registration TEXT,vin TEXT,service_date TEXT,payload_json TEXT);')
            db.close()

    def add(self, ident='1', date='2026-01-01', vin=V1, plate='B109TST', work='drums', ready=True):
        part = {'code': 'DB4299', 'manufacturer': 'TRW', 'quantity': 2, 'unit': 'pc', 'name': 'Tambur frana'}
        component = {'category': work, 'status': 'with_parts' if ready else 'labor_only', 'parts': [part] if ready else [], 'labors': [{'name': 'Inlocuit tamburi frana spate'}], 'issues': [] if ready else ['Piese lipsă'], 'ready': ready}
        row = {'transaction_id': ident, 'document_code': 'DEV-'+ident, 'registration': plate, 'vin': vin, 'service_date': date, 'invoices': [{'id': ident, 'code': 'F-'+ident}], **component, 'components': [component]}
        is_revision = work == 'revision'
        with sqlite3.connect(self.paths[0 if is_revision else 1]) as db:
            db.execute('INSERT INTO vehicle_vins VALUES (?,?)', (plate, vin))
            db.execute(f'INSERT INTO {"packages" if is_revision else "jobs"} VALUES (?,?,?,?,?)', (ident, plate, vin, date, json.dumps(row)))
        db.close()

    def lookup(self, *args, **kwargs):
        return lookup(*args, revision_db=self.paths[0], brake_db=self.paths[1], **kwargs)

    def test_exact_latest_package_with_original_identity_and_units(self):
        self.add()
        self.add('2', '2026-02-01')
        result = self.lookup('b109 tst', work='drums')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['packages'][0]['source_id'], '2')
        self.assertIn('DB4299\tTRW\t2\tpc', package_text(result['packages'][0]))

    def test_ambiguous_plate_across_both_catalogues_requires_vin(self):
        self.add()
        self.add(vin=V2, work='revision')
        result = self.lookup('B109TST')
        self.assertEqual(result['status'], 'needs_vin')
        self.assertEqual(result['packages'], [])
        self.assertEqual(self.lookup('B109TST', V1, 'drums')['status'], 'ready')

    def test_mismatched_plate_and_vin_never_returns_codes(self):
        self.add()
        self.add('2', vin=V2, plate='B100TST')
        result = self.lookup('B109TST', V2, 'drums')
        self.assertEqual(result['status'], 'identity_mismatch')
        self.assertEqual(result['packages'], [])

    def test_vin_tracks_a_changed_registration(self):
        self.add()
        self.add('2', '2026-02-01', plate='B100TST')
        result = self.lookup('B109TST', work='drums')
        self.assertEqual(result['packages'][0]['registration'], 'B100TST')

    def test_latest_incomplete_work_does_not_fall_back_to_older_codes(self):
        self.add()
        self.add('2', '2026-02-01', ready=False)
        result = self.lookup(vin=V1, work='drums')
        self.assertEqual(result['packages'][0]['status'], 'needs_review')
        self.assertEqual(result['packages'][0]['parts'], [])
        with self.assertRaises(ValueError):
            package_text(result['packages'][0])

    def test_categories_keep_their_source_packages_separate(self):
        self.add()
        self.add('2', work='pads')
        result = self.lookup(vin=V1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['packages']), 5)
        ready = {p['work']: p['source_id'] for p in result['packages'] if p['status']=='ready'}
        self.assertEqual(ready, {'pads': '2', 'drums': '1'})

    def test_invalid_or_empty_identity_and_unknown_work(self):
        for kw in ({}, {'vin': 'short'}, {'registration': 'B1', 'work': 'unknown'}):
            with self.assertRaises(ValueError):
                self.lookup(**kw)

    def test_missing_database_does_not_create_a_new_one(self):
        missing = Path(self.tmp.name)/'missing.db'
        with self.assertRaises(FileNotFoundError):
            lookup(vin=V1, revision_db=missing, brake_db=self.paths[1])
        self.assertFalse(missing.exists())

    def test_advisor_vehicle_selection_unions_categories_and_preserves_vins(self):
        self.add()
        self.add('2', work='revision')
        self.add('3', vin=V2, work='pads')
        result = vehicles('b109 tst', revision_db=self.paths[0], brake_db=self.paths[1])
        self.assertEqual(len(result['vehicles']), 2)
        first = next(v for v in result['vehicles'] if v['vin']==V1)
        self.assertEqual(first['works'], ['revision', 'drums'])
        self.assertEqual(first['registration'], 'B109TST')
        exact = vehicles(V2.lower(), revision_db=self.paths[0], brake_db=self.paths[1])
        self.assertEqual([v['vin'] for v in exact['vehicles']], [V2])

    def test_advisor_search_limit_and_empty_query(self):
        self.assertEqual(vehicles('', revision_db=self.paths[0], brake_db=self.paths[1])['vehicles'], [])
        self.add()
        self.add('2', vin=V2)
        result = vehicles('B271', limit=1, revision_db=self.paths[0], brake_db=self.paths[1])
        self.assertEqual(len(result['vehicles']), 1)
        self.assertTrue(result['has_more'])


if __name__ == '__main__':
    unittest.main()
