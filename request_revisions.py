"""Keep one active Dashboard entry for an explicitly linked completion.

Archiving here changes Dashboard visibility, never the physical work status,
appointments, DSM documents, offers or their existing approval.
"""
from contextlib import closing
import re
import sqlite3
import unicodedata


def schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS request_archive(request_id INTEGER PRIMARY KEY,archived_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('''CREATE TABLE IF NOT EXISTS request_revisions(
        old_request_id INTEGER PRIMARY KEY,new_request_id INTEGER NOT NULL,
        previous_status TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')


def completion_text(value):
    text=''.join(c for c in unicodedata.normalize('NFKD',str(value or '').lower()) if not unicodedata.combining(c))
    return bool(re.match(r'\s*completare\s+(?:la\s+)?(?:solicitarea|cererea)\s+(?:existenta|anterioara|#\s*\d+)',text))


def normalized(value):
    return re.sub(r'[^A-Z0-9]','',str(value or '').upper())


def reconcile(conn,new_id,old_id=None,*,apply=True):
    explicit=old_id is not None
    conn.row_factory=sqlite3.Row
    new=conn.execute('SELECT * FROM service_requests WHERE id=?',(int(new_id),)).fetchone()
    if not new or not explicit and not completion_text(new['symptom']):
        return None
    schema(conn)
    prior=conn.execute('SELECT * FROM request_revisions WHERE new_request_id=? ORDER BY old_request_id DESC LIMIT 1',(int(new_id),)).fetchone()
    if prior and not explicit:
        if not prior['active']:
            return None  # An explicit manual restore wins over automatic reconciliation.
        old_id=prior['old_request_id']
    if old_id is None:
        row=conn.execute('''SELECT id FROM service_requests WHERE session_id=? AND id<?
            AND id NOT IN (SELECT request_id FROM request_archive)
            AND status NOT IN ('rezolvat','anulat','finalizat') ORDER BY id DESC LIMIT 1''',
            (new['session_id'],int(new_id))).fetchone()
        if not row:
            return None
        old_id=row['id']
    old=conn.execute('SELECT * FROM service_requests WHERE id=?',(int(old_id),)).fetchone()
    valid=bool(old and int(old_id)<int(new_id)
        and new['session_id'] and old['session_id']==new['session_id']
        and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',normalized(new['vin']))
        and normalized(old['vin'])==normalized(new['vin'])
        and normalized(new['registration']) and normalized(old['registration'])==normalized(new['registration'])
        and len(re.sub(r'\D','',str(new['phone'] or '')))>=9
        and re.sub(r'\D','',str(old['phone'] or ''))[-9:]==re.sub(r'\D','',str(new['phone'] or ''))[-9:])
    if not valid:
        if explicit:
            raise ValueError('Legătura necesită aceeași conversație, mașină, VIN și același contact confirmat.')
        return None
    existing=conn.execute('SELECT new_request_id FROM request_revisions WHERE old_request_id=?',(int(old_id),)).fetchone()
    if existing and existing['new_request_id']!=int(new_id):
        if explicit:
            raise ValueError('Cererea veche are deja o altă completare asociată.')
        return None
    result=dict(old_request_id=int(old_id),new_request_id=int(new_id),previous_status=old['status'],
                archived=bool(apply),reason=f'Înlocuită în Dashboard de cererea #{new_id}')
    if apply:
        conn.execute('''INSERT INTO request_revisions(old_request_id,new_request_id,previous_status)
            VALUES(?,?,?) ON CONFLICT(old_request_id) DO UPDATE SET active=1''',
            (int(old_id),int(new_id),str(old['status'] or 'nou')))
        conn.execute('INSERT OR IGNORE INTO request_archive(request_id) VALUES(?)',(int(old_id),))
    return result


def link_completion(db_path,new_id,old_id=None,*,apply=True):
    with closing(sqlite3.connect(db_path)) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        return reconcile(conn,new_id,old_id,apply=apply)


def metadata(db_path):
    result={}
    with closing(sqlite3.connect(db_path)) as conn,conn:
        schema(conn)
        for old,new in conn.execute('SELECT old_request_id,new_request_id FROM request_revisions WHERE active=1'):
            result.setdefault(old,{})['replacement_request_id']=new
            result.setdefault(new,{})['replaces_request_id']=old
    return result
