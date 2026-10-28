"""Incremental JSONL import and indexed, paginated conversation reads.

JSONL remains an append-only recovery log. Every legacy line (including invalid
ones) is retained verbatim in SQLite; checkpoints commit with their records.
Memory during import is proportional to one record, not to the history size.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from zoneinfo import ZoneInfo

LOCK = threading.RLock()
MAX_PAGE = 200


def canonical_phone(value):
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) == 10 and digits.startswith('0'):
        return '40' + digits[1:]
    if len(digits) == 9 and digits.startswith('7'):
        return '40' + digits
    return digits


def timestamp_seconds(value):
    try:
        stamp = datetime.fromisoformat(str(value or ''))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo(os.getenv('TZ', 'Europe/Bucharest')))
        return stamp.timestamp()
    except (ValueError, TypeError):
        return None


class ConversationLog:
    def __init__(self, log_path):
        self.log = Path(log_path)
        self.database = self.log.with_name('conversation_log_index.sqlite3')

    def connect(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.database, timeout=30)
        try:
            self._initialize(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _initialize(conn):
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA cache_size=-2048')
        conn.executescript('''
          CREATE TABLE IF NOT EXISTS log_sources (
            id INTEGER PRIMARY KEY, path TEXT NOT NULL, identity TEXT NOT NULL,
            offset INTEGER NOT NULL DEFAULT 0, prefix_length INTEGER NOT NULL DEFAULT 0,
            prefix_hash TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
          CREATE UNIQUE INDEX IF NOT EXISTS active_log_source ON log_sources(path) WHERE active=1;
          CREATE TABLE IF NOT EXISTS log_messages (
            id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, byte_offset INTEGER NOT NULL,
            phone TEXT, case_id TEXT, direction TEXT, timestamp_utc REAL, raw BLOB NOT NULL,
            UNIQUE(source_id, byte_offset));
          CREATE INDEX IF NOT EXISTS log_case_phone ON log_messages(case_id, phone, id);
          CREATE INDEX IF NOT EXISTS log_phone ON log_messages(phone, id);
          CREATE INDEX IF NOT EXISTS log_inbound ON log_messages(phone, direction, id);
        ''')

    def _sync(self, conn):
        if not self.log.exists():
            return
        with self.log.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            identity = f'{stat.st_dev}:{stat.st_ino}'
            source = conn.execute('SELECT id,identity,offset,prefix_length,prefix_hash FROM log_sources '
                                  'WHERE path=? AND active=1', (str(self.log.resolve()),)).fetchone()
            if source:
                ident, old_identity, offset, length, digest = source
                prefix = stream.read(length)
                valid = (old_identity == identity and stat.st_size >= offset
                         and hashlib.sha256(prefix).hexdigest() == digest)
            else:
                valid = False
            if not valid:
                conn.execute('UPDATE log_sources SET active=0 WHERE path=?', (str(self.log.resolve()),))
                ident = conn.execute('INSERT INTO log_sources(path,identity,prefix_hash) VALUES (?,?,?)',
                                     (str(self.log.resolve()), identity, hashlib.sha256(b'').hexdigest())).lastrowid
                offset = 0
            stream.seek(offset)
            count = 0
            while True:
                start = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                try:
                    row = json.loads(raw)
                    if not isinstance(row, dict):
                        row = {}
                except (ValueError, UnicodeDecodeError):
                    if not raw.endswith(b'\n'):
                        break  # A writer has not finished this record yet.
                    row = {}
                conn.execute('INSERT OR IGNORE INTO log_messages '
                             '(source_id,byte_offset,phone,case_id,direction,timestamp_utc,raw) VALUES (?,?,?,?,?,?,?)',
                             (ident, start, canonical_phone(row.get('phone')),
                              str(row.get('case_id') or '').strip(), str(row.get('direction') or ''),
                              timestamp_seconds(row.get('timestamp')), raw))
                offset = stream.tell()
                count += 1
                if count % 500 == 0:
                    conn.execute('UPDATE log_sources SET offset=? WHERE id=?', (offset, ident))
                    conn.commit()
            stream.seek(0)
            prefix = stream.read(min(4096, offset))
            conn.execute('UPDATE log_sources SET offset=?,prefix_length=?,prefix_hash=? WHERE id=?',
                         (offset, len(prefix), hashlib.sha256(prefix).hexdigest(), ident))
            conn.commit()

    def sync(self):
        with LOCK, closing(self.connect()) as conn:
            self._sync(conn)

    def append(self, row):
        with LOCK:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            separator = b''
            if self.log.exists() and self.log.stat().st_size:
                with self.log.open('rb') as stream:
                    stream.seek(-1, os.SEEK_END)
                    separator = b'\n' if stream.read(1) != b'\n' else b''
            with self.log.open('ab') as stream:
                stream.write(separator + json.dumps(row, ensure_ascii=False).encode('utf-8') + b'\n')
            # If SQLite is unavailable, the durable JSONL entry is recovered by
            # the next sync rather than losing an incoming/outgoing message.
            with closing(self.connect()) as conn:
                self._sync(conn)

    def page(self, request, *, before=None, limit=100):
        self.sync()
        number = canonical_phone(request.get('phone') or request.get('telefon'))
        case = str(request.get('session_id') or request.get('conversation_id') or '').strip()
        limit = max(1, min(int(limit), MAX_PAGE))
        with closing(self.connect()) as conn:
            strict_sql, args = 'case_id=?', [case]
            if number:
                strict_sql += ' AND phone=?'
                args.append(number)
            strict = bool(case and conn.execute('SELECT 1 FROM log_messages WHERE '+strict_sql+' LIMIT 1', args).fetchone())
            if strict:
                where = strict_sql
            elif number:
                where, args = 'phone=?', [number]
            else:
                return [], False
            if before is not None:
                where += ' AND id<?'
                args.append(int(before))
            rows = conn.execute('SELECT id,raw FROM log_messages WHERE '+where+' ORDER BY id DESC LIMIT ?',
                                [*args, limit]).fetchall()
        messages = []
        for ident, raw in reversed(rows):
            row = json.loads(raw)
            row['_message_id'] = ident
            messages.append(row)
        return messages, strict

    def has_window(self, phone, *, hours=24, now=None):
        number = canonical_phone(phone)
        if not number:
            return False
        self.sync()
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT timestamp_utc FROM log_messages WHERE phone=? AND direction='client' "
                               'AND timestamp_utc IS NOT NULL ORDER BY id DESC LIMIT 1', (number,)).fetchone()
        now = now or datetime.now(timezone.utc)
        return bool(row and row[0] >= (now - timedelta(hours=hours)).timestamp())
