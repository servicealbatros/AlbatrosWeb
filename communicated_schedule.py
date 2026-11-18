"""Preserve the local assignments actually communicated to a mechanic."""
from contextlib import closing
from datetime import date
import json
import re
import sqlite3


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS scheduler_communicated_appointments (
        appointment_id INTEGER PRIMARY KEY, technician_id INTEGER NOT NULL,
        start_at TEXT NOT NULL, end_at TEXT NOT NULL, recorded_at TEXT DEFAULT CURRENT_TIMESTAMP)''')


def protected_ids(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_communicated_appointments'").fetchone():
        return set()
    return {r[0] for r in conn.execute('SELECT appointment_id FROM scheduler_communicated_appointments')}


def record_sent(db_path, technician_id, appointments):
    with closing(sqlite3.connect(db_path)) as conn:
        initialize(conn)
        conn.executemany('''INSERT OR REPLACE INTO scheduler_communicated_appointments
            (appointment_id,technician_id,start_at,end_at) VALUES (?,?,?,?)''',
            [(a['id'], technician_id, a['start_at'], a['end_at']) for a in appointments])
        conn.commit()


def restore(db_path, technician_id, day, entries):
    """Explicit master recovery of exact references supplied from a sent schedule.

    This restores an existing allocation, without treating it as a new capacity
    suggestion. No DSM write, appointment creation, or outbound message occurs.
    """
    date.fromisoformat(day)
    if not isinstance(entries, list) or not 1 <= len(entries) <= 40:
        raise ValueError('Lista programărilor comunicate este invalidă.')
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        delivery = conn.execute('''SELECT status FROM scheduler_mechanic_schedule_deliveries
            WHERE schedule_date=? AND technician_id=?''', (day, technician_id)).fetchone()
        if not delivery or delivery['status'] not in {'accepted_by_meta', 'sent', 'delivered', 'read'}:
            raise ValueError('Nu există o trimitere confirmată pentru acest profil și această zi.')
        tech = conn.execute('SELECT department FROM scheduler_technicians WHERE id=? AND active=1', (technician_id,)).fetchone()
        if not tech:
            raise ValueError('Profilul nu este activ.')
        rows = []
        ids = set()
        for item in entries:
            ident = int(item['id'])
            row = conn.execute('SELECT * FROM scheduler_appointments WHERE id=?', (ident,)).fetchone()
            normalize = lambda v: re.sub(r'[^A-Z0-9]', '', str(v or '').upper())
            if (ident in ids or not row or row['status'] != 'programat'
                    or str(row['start_at'])[:10] != day
                    or row['start_at'] != item['start_at'] or row['end_at'] != item['end_at']
                    or row['dsm_appointment_id'] != item['dsm_appointment_id']
                    or normalize(row['registration']) != normalize(item['registration'])):
                raise ValueError('O programare nu mai corespunde exact mesajului comunicat.')
            ids.add(ident)
            rows.append(dict(row))
        ordered = sorted(rows, key=lambda r: r['start_at'])
        if any(a['end_at'] > b['start_at'] for a, b in zip(ordered, ordered[1:])):
            raise ValueError('Programările comunicate se suprapun.')
        other = conn.execute("SELECT id,start_at,end_at FROM scheduler_appointments WHERE technician_id=? AND status!='anulat'", (technician_id,)).fetchall()
        if any(a['id'] not in ids and a['start_at'] < r['end_at'] and a['end_at'] > r['start_at'] for a in other for r in rows):
            raise ValueError('Profilul are între timp alte lucrări în aceleași intervale.')
        initialize(conn)
        conn.execute('''CREATE TABLE IF NOT EXISTS scheduler_communicated_recoveries (
            id INTEGER PRIMARY KEY, recovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            technician_id INTEGER, schedule_date TEXT, before_json TEXT NOT NULL)''')
        before = [{k: r[k] for k in ('id', 'technician_id', 'start_at', 'end_at', 'source', 'department')} for r in rows]
        conn.execute('INSERT INTO scheduler_communicated_recoveries(technician_id,schedule_date,before_json) VALUES (?,?,?)',
                     (technician_id, day, json.dumps(before)))
        for row in rows:
            conn.execute("UPDATE scheduler_appointments SET technician_id=?,department=?,source='local_schedule_override',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (technician_id, tech['department'], row['id']))
            conn.execute('INSERT OR REPLACE INTO scheduler_communicated_appointments(appointment_id,technician_id,start_at,end_at) VALUES (?,?,?,?)',
                         (row['id'], technician_id, row['start_at'], row['end_at']))
        conn.commit()
        return dict(ok=True, restored_ids=sorted(ids), technician_id=technician_id, date=day)
