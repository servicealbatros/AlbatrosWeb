from contextlib import closing
import gc
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import database
from request_revisions import link_completion,metadata,schema


class RequestRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup);self.addCleanup(gc.collect)
        self.path=Path(self.temp.name)/'requests.db'
        for mock in (patch.object(database,'DB_PATH',self.path),patch.object(database,'known_dsm_vehicle',return_value=None)):
            mock.start();self.addCleanup(mock.stop)
        database.init_db()
        self.base=dict(session_id='whatsapp:test:case:1',phone='40700100011',registration='B112TST',
            vin='WV100000000000008',symptom='Check Engine, schimb ulei și filtre')
        self.old=database.create_request(self.base)
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute("UPDATE service_requests SET status='programat' WHERE id=?",(self.old,))
            conn.execute('CREATE TABLE scheduler_appointments(id INTEGER PRIMARY KEY,request_id INTEGER,status TEXT)')
            conn.execute("INSERT INTO scheduler_appointments VALUES(1,?,'programat')",(self.old,))

    def completion(self,**changes):
        return database.create_request({**self.base,'symptom':'Completare la solicitarea existentă: Front Assist; anterior: Check Engine, ulei și filtre',**changes})

    def archived(self):
        with closing(sqlite3.connect(self.path)) as conn,conn:
            schema(conn)
            return [r[0] for r in conn.execute('SELECT request_id FROM request_archive')]

    def test_completion_replaces_only_dashboard_visibility_and_preserves_scheduled_work(self):
        new=self.completion()
        self.assertNotEqual(new,self.old);self.assertEqual(self.archived(),[self.old])
        links=metadata(self.path)
        self.assertEqual(links[self.old]['replacement_request_id'],new)
        self.assertEqual(links[new]['replaces_request_id'],self.old)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM service_requests WHERE id=?',(self.old,)).fetchone()[0],'programat')
            self.assertEqual(conn.execute('SELECT request_id,status FROM scheduler_appointments').fetchone(),(self.old,'programat'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0],2)
        link_completion(self.path,new,self.old)
        self.assertEqual(self.archived(),[self.old])

    def test_unrelated_job_on_same_vehicle_is_not_archived(self):
        database.create_request({**self.base,'symptom':'Înlocuire anvelope'})
        self.assertEqual(self.archived(),[])

    def test_different_vehicle_session_or_contact_never_hides_previous_request(self):
        for changes in ({'vin':'WBA00000000000002'},{'registration':'B101TST'},
                        {'session_id':'whatsapp:test:case:other'},{'phone':'40700100012'}):
            with closing(sqlite3.connect(self.path)) as conn,conn:
                conn.execute('DELETE FROM service_requests WHERE id>?',(self.old,))
            new=database.create_request({**self.base,'symptom':'Altă lucrare',**changes})
            with self.assertRaises(ValueError):link_completion(self.path,new,self.old)
        self.assertEqual(self.archived(),[])

    def test_missing_vin_keeps_old_visible_until_vehicle_is_confirmed(self):
        new=self.completion(vin=None)
        self.assertEqual(self.archived(),[])
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute('UPDATE service_requests SET vin=? WHERE id=?',(self.base['vin'],new))
        link_completion(self.path,new)
        self.assertEqual(self.archived(),[self.old])

    def test_preview_and_manual_restore_do_not_rearchive_old_request(self):
        new=database.create_request({**self.base,'symptom':'Front Assist'})
        result=link_completion(self.path,new,self.old,apply=False)
        self.assertFalse(result['archived']);self.assertEqual(self.archived(),[])
        link_completion(self.path,new,self.old)
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute('DELETE FROM request_archive WHERE request_id=?',(self.old,))
            conn.execute('UPDATE request_revisions SET active=0 WHERE old_request_id=?',(self.old,))
            conn.execute('UPDATE service_requests SET symptom=? WHERE id=?',('Completare la solicitarea existentă: Front Assist',new))
        self.assertIsNone(link_completion(self.path,new));self.assertEqual(self.archived(),[])

    def test_draft_update_stays_on_one_request_without_self_replacement(self):
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute("UPDATE service_requests SET status='nou' WHERE id=?",(self.old,))
        new=self.completion()
        self.assertEqual(new,self.old);self.assertEqual(self.archived(),[])


if __name__=='__main__':unittest.main()
