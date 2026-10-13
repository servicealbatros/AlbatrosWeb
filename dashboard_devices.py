"""Server-side dashboard access registry; contains no supplier credentials."""
import secrets
import sqlite3
import time
from contextlib import contextmanager


class DashboardDevices:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT, ip TEXT, state TEXT, seen REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, device TEXT, expires REAL, revoked INTEGER DEFAULT 0)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, device, name, ip):
        with self.connect() as db:
            row = db.execute('SELECT * FROM devices WHERE id=?', (device,)).fetchone()
            if row:
                return device, row['state']
            device = secrets.token_urlsafe(32)
            db.execute('INSERT INTO devices VALUES (?,?,?,?,?)', (device, name[:100], ip, 'pending', time.time()))
            return device, 'pending'

    def issue(self, device, expires):
        sid = secrets.token_urlsafe(32)
        with self.connect() as db:
            row = db.execute('SELECT state FROM devices WHERE id=?', (device,)).fetchone()
            if not row or row['state'] != 'active':
                raise PermissionError('Calculatorul necesită activarea de către administrator.')
            db.execute('INSERT INTO sessions(id,device,expires) VALUES (?,?,?)', (sid, device, expires))
        return sid

    def valid(self, sid):
        with self.connect() as db:
            row = db.execute("SELECT s.device FROM sessions s JOIN devices d ON d.id=s.device WHERE s.id=? AND s.revoked=0 AND s.expires>? AND d.state='active'", (sid, time.time())).fetchone()
            if not row:
                return False
            db.execute('UPDATE devices SET seen=? WHERE id=?', (time.time(), row['device']))
            return True

    def change(self, device, active):
        with self.connect() as db:
            db.execute('UPDATE devices SET state=? WHERE id=?', ('active' if active else 'revoked', device))
            db.execute('UPDATE sessions SET revoked=1 WHERE device=?', (device,))

    def revoke_all(self):
        with self.connect() as db:
            db.execute("UPDATE devices SET state='revoked'")
            db.execute('UPDATE sessions SET revoked=1')

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM devices ORDER BY seen DESC')]
