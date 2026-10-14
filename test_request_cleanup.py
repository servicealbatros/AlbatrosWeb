import ast
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


class CleanupTest(unittest.TestCase):
    def test_archive_and_requests_removed_dsm_preserved_with_recovery(self):
        tree = ast.parse(Path('app.py').read_text(encoding='utf-8-sig'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'master_clear_requests')
        function.decorator_list = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'service_requests.db'
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript('''
                    CREATE TABLE service_requests(id INTEGER PRIMARY KEY);
                    INSERT INTO service_requests VALUES(38),(39);
                    CREATE TABLE request_archive(request_id INTEGER);
                    INSERT INTO request_archive VALUES(39);
                    CREATE TABLE scheduler_appointments(id INTEGER,request_id INTEGER,source TEXT);
                    INSERT INTO scheduler_appointments VALUES(1,38,'albatros_safe_local'),(2,39,'dsm_readonly'),(3,NULL,'dsm_readonly');
                    CREATE TABLE scheduler_mechanic_messages(appointment_id INTEGER);
                    INSERT INTO scheduler_mechanic_messages VALUES(1),(2);
                    CREATE TABLE dashboard_yard_orders(service_request_id INTEGER);
                    INSERT INTO dashboard_yard_orders VALUES(38);
                ''')
                conn.commit()
            offers = root / 'offering' / 'requests'
            offers.mkdir(parents=True)
            (offers / 'request_38_products.json').write_text('{}')
            (root / 'dsm_history.sqlite3').write_bytes(b'preserve')
            (root / 'offer_flow_state.json').write_text('{"phone": {"request_id": 38}}')
            namespace = dict(Request=object, _master_access=lambda r: None,
                POLICY=SimpleNamespace(robot_enabled=False),
                _dsm_week_import_lock=SimpleNamespace(locked=lambda: False),
                DATA_DIR=root, OFFER_REQUESTS_DIR=offers, DB_PATH=db, datetime=datetime,
                uuid4=uuid4, sqlite3=sqlite3, closing=closing, re=re,
                _active_case_by_sender={}, _plate_by_sender={}, _pending_new_request_by_sender={})
            exec(compile(ast.Module(body=[function], type_ignores=[]), 'cleanup', 'exec'), namespace)
            result = namespace['master_clear_requests'](SimpleNamespace(headers={'X-Human-Action': 'clear-all-requests-including-archive'}))
            self.assertEqual(result['deleted_requests'], 2)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM request_archive').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT id,request_id FROM scheduler_appointments ORDER BY id').fetchall(), [(2,None),(3,None)])
            with closing(sqlite3.connect(root / 'backups' / result['backup'] / 'service_requests.db')) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0], 2)
            self.assertEqual((root / 'dsm_history.sqlite3').read_bytes(), b'preserve')
            self.assertEqual(json.loads((root / 'offer_flow_state.json').read_text()), {})
            self.assertFalse((offers / 'request_38_products.json').exists())


if __name__ == '__main__':
    unittest.main()

