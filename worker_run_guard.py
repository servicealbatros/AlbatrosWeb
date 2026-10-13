"""Reject writes from superseded supplier workers, across processes."""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from uuid import uuid4

@contextmanager
def registry(path):
    path=Path(path).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path.parent/'worker_runs.db',timeout=10)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS runs (path TEXT PRIMARY KEY, token TEXT NOT NULL)')
        db.commit()
        with db:
            db.execute('BEGIN IMMEDIATE')
            yield db,path
    finally:
        db.close()

def register_worker_run(paths):
    token=uuid4().hex
    for path in paths:
        with registry(path) as (db,path):
            db.execute('INSERT OR REPLACE INTO runs(path,token) VALUES (?,?)',(str(path),token))
    return token

def write_worker_result(path,data,token):
    if not token:
        path=Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return True
    with registry(path) as (db,path):
        row=db.execute('SELECT token FROM runs WHERE path=?',(str(path),)).fetchone()
        if not row or row[0]!=token:
            return False
        temporary=path.with_name(path.name+'.'+token+'.tmp')
        temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)
        return True
