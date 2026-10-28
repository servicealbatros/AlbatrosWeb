from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from conversation_log_index import ConversationLog


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / 'conversation.jsonl'
        self.store = ConversationLog(self.log)

    def write(self, rows):
        self.log.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def row(self, text='message', phone='0700100001', case='case-a', direction='client', timestamp=None):
        return dict(text=text, phone=phone, case_id=case, direction=direction,
                    timestamp=timestamp or datetime.now(timezone.utc).isoformat())

    def test_isolation_normalization_pagination_and_chronology(self):
        rows = [self.row(str(i)) for i in range(350)]
        rows += [self.row('different case', case='case-b'), self.row('other customer', phone='0700100002')]
        self.write(rows)
        request = {'phone': '+40 700 100 001', 'session_id': 'case-a'}
        with patch.object(Path, 'read_text', side_effect=AssertionError('whole-file read')):
            first, strict = self.store.page(request, limit=100)
            second, _ = self.store.page(request, limit=100, before=first[0]['_message_id'])
        self.assertTrue(strict)
        self.assertEqual([x['text'] for x in first], [str(i) for i in range(250, 350)])
        self.assertEqual([x['text'] for x in second], [str(i) for i in range(150, 250)])
        fallback, strict = self.store.page({'phone': '700100001', 'session_id': 'missing'}, limit=10000)
        self.assertFalse(strict)
        self.assertEqual(len(fallback), 200)
        self.assertNotIn('other customer', [x['text'] for x in fallback])
        self.assertEqual(self.store.page({}), ([], False))

    def test_repeatable_import_preserves_invalid_and_duplicate_lines(self):
        row = self.row()
        raw = (json.dumps(row) + '\nnot json\n' + json.dumps(row) + '\n').encode()
        self.log.write_bytes(raw)
        self.store.sync()
        self.store.sync()
        with closing(sqlite3.connect(self.store.database)) as conn:
            stored = conn.execute('SELECT raw FROM log_messages ORDER BY id').fetchall()
        self.assertEqual(b''.join(row[0] for row in stored), raw)
        self.assertEqual(len(self.store.page({'phone': row['phone']})[0]), 2)

    def test_interrupted_checkpoint_does_not_duplicate_500_committed_rows(self):
        self.write([self.row(str(i)) for i in range(600)])
        connect = self.store.connect
        class CrashAfterCheckpoint:
            def __init__(self):
                self.conn = connect()
            def __getattr__(self, name):
                return getattr(self.conn, name)
            def commit(self):
                self.conn.commit()
                raise RuntimeError('power loss after checkpoint')
        with patch.object(self.store, 'connect', side_effect=CrashAfterCheckpoint), self.assertRaises(RuntimeError):
            self.store.sync()
        self.store.sync()
        with closing(connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM log_messages').fetchone()[0], 600)

    def test_rotation_append_and_incomplete_tail_preserve_records(self):
        self.write([self.row('old')])
        self.store.sync()
        self.log.write_text(json.dumps(self.row('new')) + '\n', encoding='utf-8')
        self.store.sync()
        with self.log.open('ab') as stream:
            stream.write(b'{"text":')
        self.store.sync()
        with self.log.open('ab') as stream:
            stream.write(b'"tail", "phone":"0700100001", "case_id":"case-a"}\n')
        self.store.sync()
        self.store.append(self.row('appended'))
        rows, _ = self.store.page({'phone': '0700100001'})
        self.assertEqual([row['text'] for row in rows], ['old', 'new', 'tail', 'appended'])

    def test_whatsapp_window_uses_only_latest_valid_inbound_for_same_phone(self):
        now = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)
        self.write([self.row(timestamp=(now-timedelta(hours=25)).isoformat()),
                    self.row(direction='robot', timestamp=now.isoformat()),
                    self.row(phone='0700100002', timestamp=now.isoformat())])
        self.assertFalse(self.store.has_window('40700100001', now=now))
        self.store.append(self.row(timestamp=(now-timedelta(hours=23)).isoformat()))
        self.store.append(self.row(timestamp='malformed'))
        self.assertTrue(self.store.has_window('+40 700 100 001', now=now))
        self.assertFalse(self.store.has_window('0700100003', now=now))

    def test_index_failure_does_not_lose_appended_message(self):
        with patch.object(self.store, 'connect', side_effect=sqlite3.OperationalError('database locked')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.append(self.row('recoverable'))
        self.assertEqual(self.store.page({'phone':'0700100001'})[0][0]['text'],'recoverable')


if __name__ == '__main__':
    unittest.main()
