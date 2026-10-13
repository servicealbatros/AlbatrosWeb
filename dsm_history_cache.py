"""Local DSM mirror. VIN separates vehicles; historical prices are never live quotes."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3

DATA=Path(os.getenv('ALBATROS_DATA_DIR',Path(__file__).parent/'data')).resolve()
DEFAULT_DB=DATA/'dsm_history.sqlite3'

def plate(value):
    return re.sub(r'[^A-Z0-9]','',str(value or '').upper())

def valid_vin(value):
    return bool(re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',str(value or '')))

@contextmanager
def connect(path=DEFAULT_DB):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('\nCREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY,registration TEXT,car_id TEXT,vin TEXT,status TEXT,service_date TEXT,header_json TEXT,header_hash TEXT,payload_json TEXT,complete INTEGER DEFAULT 0,error TEXT,imported_at TEXT);\nCREATE INDEX IF NOT EXISTS tx_vin ON transactions(vin,status,service_date);\nCREATE INDEX IF NOT EXISTS tx_plate ON transactions(registration,service_date);\nCREATE TABLE IF NOT EXISTS vehicles(vin TEXT PRIMARY KEY,car_id TEXT,registration TEXT,identity_json TEXT,updated_at TEXT);\nCREATE INDEX IF NOT EXISTS car_plate ON vehicles(registration);\nCREATE TABLE IF NOT EXISTS lines(transaction_id TEXT,row_id TEXT,kind TEXT,code TEXT,manufacturer TEXT,name TEXT,quantity REAL,unit TEXT,vin TEXT,PRIMARY KEY(transaction_id,row_id));\nCREATE INDEX IF NOT EXISTS line_vin_code ON lines(vin,code);\nCREATE TABLE IF NOT EXISTS import_state(key TEXT PRIMARY KEY,value TEXT);\n')
        db.execute('CREATE TABLE IF NOT EXISTS local_vehicle_identity(vin TEXT PRIMARY KEY,registration TEXT NOT NULL,profile_json TEXT NOT NULL,source TEXT NOT NULL,verified_at TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS local_identity_plate ON local_vehicle_identity(registration)')
        db.commit()
        with db:yield db
    finally:db.close()

def state(path=DEFAULT_DB,**updates):
    with connect(path) as db:
        for key,value in updates.items():
            db.execute('INSERT OR REPLACE INTO import_state VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))
        info={r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM import_state')}
        counts=dict(db.execute('SELECT COUNT(*) total,SUM(complete) imported FROM transactions').fetchone())
        info.update(total=counts['total'],imported=counts['imported'] or 0,vehicles=db.execute('SELECT COUNT(*) FROM (SELECT vin FROM vehicles UNION SELECT vin FROM local_vehicle_identity)').fetchone()[0],parts=db.execute("SELECT COUNT(*) FROM lines WHERE kind='part'").fetchone()[0],errors=db.execute("SELECT COUNT(*) FROM transactions WHERE error IS NOT NULL").fetchone()[0])
        return info

def store_headers(rows,path=DEFAULT_DB):
    with connect(path) as db:
        for row in rows:
            ident=str(row.get('transaction_id') or row.get('id') or '')
            if not ident.isdigit():continue
            raw=json.dumps(row,ensure_ascii=False,sort_keys=True)
            digest=hashlib.sha256(raw.encode()).hexdigest()
            db.execute('INSERT INTO transactions(id,registration,car_id,status,service_date,header_json,header_hash) VALUES (?,?,?,?,?,?,?)\nON CONFLICT(id) DO UPDATE SET registration=excluded.registration,status=excluded.status,service_date=excluded.service_date,header_json=excluded.header_json,\ncomplete=CASE WHEN transactions.header_hash=excluded.header_hash THEN transactions.complete ELSE 0 END,error=NULL,header_hash=excluded.header_hash',(ident,plate(row.get('car_no') or row.get('car_number')),str(row.get('car_id') or ''),str(row.get('status') or row.get('transaction_head__status') or ''),str(row.get('record_date') or row.get('created_at') or ''),raw,digest))

def store_detail(detail,path=DEFAULT_DB):
    ident=str(detail['transaction_id']);vin=str(detail.get('vin') or '').upper()
    if not valid_vin(vin):vin=''
    with connect(path) as db:
        db.execute('UPDATE transactions SET vin=?,car_id=?,registration=?,status=?,payload_json=?,complete=1,error=NULL,imported_at=? WHERE id=?',(vin,str(detail.get('dsm_car_id') or ''),plate(detail.get('registration')),detail.get('status'),json.dumps(detail,ensure_ascii=False),datetime.now().isoformat(),ident))
        db.execute('DELETE FROM lines WHERE transaction_id=?',(ident,))
        for kind in ('parts','labors','other_rows'):
            for index,row in enumerate(detail.get(kind) or []):
                db.execute('INSERT OR REPLACE INTO lines VALUES (?,?,?,?,?,?,?,?,?)',(ident,str(row.get('row_id') or f'{kind}:{index}'),'part' if kind=='parts' else 'labor' if kind=='labors' else row.get('kind','other'),row.get('code'),row.get('manufacturer'),row.get('name'),row.get('quantity'),row.get('unit'),vin))
        if vin:
            db.execute('INSERT OR REPLACE INTO vehicles VALUES (?,?,?,?,?)',(vin,str(detail.get('dsm_car_id') or ''),plate(detail.get('registration')),json.dumps(detail,ensure_ascii=False),datetime.now().isoformat()))

def store_vehicle_identity(vehicle,source,path=DEFAULT_DB):
    vin=str(vehicle.get('vin') or '').strip().upper()
    registration=plate(vehicle.get('registration'))
    if not valid_vin(vin) or not registration or not vehicle.get('confirmed'):
        return False
    if source.startswith('DSM') and not vehicle.get('history_complete',False):
        return False
    profile=dict(vehicle)
    profile['verified_at']=datetime.now().isoformat()
    profile.update(vin=vin,registration=registration,source=source)
    profile['transactions']=[t for t in profile.get('transactions') or [] if str(t.get('vin') or '').upper()==vin]
    with connect(path) as db:
        db.execute('INSERT OR REPLACE INTO local_vehicle_identity VALUES (?,?,?,?,?)',(vin,registration,json.dumps(profile,ensure_ascii=False),source,datetime.now().isoformat()))
    return True


def _local_identity(vin,path=DEFAULT_DB):
    with connect(path) as db:
        row=db.execute('SELECT profile_json FROM local_vehicle_identity WHERE vin=?',(vin,)).fetchone()
        return json.loads(row[0]) if row else None


def lookup_vin(vin,path=DEFAULT_DB):
    vin=str(vin or '').strip().upper()
    if not valid_vin(vin):return None
    info=state(path)
    # A partial mirror must never be presented as the complete vehicle history.
    profile=_local_identity(vin,path)
    if info.get('phase')!='complete':return profile
    with connect(path) as db:
        rows=db.execute('SELECT payload_json FROM transactions WHERE vin=? AND complete=1 ORDER BY CAST(id AS INTEGER) DESC',(vin,)).fetchall()
        if not rows:return profile
        details=[json.loads(r[0]) for r in rows]
        current=next((r for r in details if r.get('registered_car_vin')==vin),None)
        if current is None:return profile
        result={k:current.get(k) for k in ('registration','vin','vehicle','customer_name','customer_phone','mileage_km','year','engine_or_variant','power_kw','displacement_cc','dsm_car_id','haynes_submodel_id','tecrmi_type_id','tecdoc_type_id')}
        if profile and (str(profile.get('source') or '').startswith('Talon') or str(profile.get('verified_at') or '')>str(info.get('finished_at') or '')):
            result.update({k:profile[k] for k in result if profile.get(k) not in (None,'')})
        result.update(found=True,confirmed=True,source='DSM local VIN cache',transactions=details,history_total=len(details),history_read=len(details),history_complete=True,cache_synced_at=info.get('finished_at'))
        return result

def lookup_registration(value,path=DEFAULT_DB):
    registration=plate(value)
    info=state(path)
    with connect(path) as db:
        identities=db.execute('SELECT vin FROM local_vehicle_identity WHERE registration=?',(registration,)).fetchall()
        if len(identities)>1:
            return None
        identity_vin=identities[0][0] if identities else None
        row=(db.execute("SELECT payload_json FROM transactions WHERE registration=? AND complete=1 AND vin!='' ORDER BY CAST(id AS INTEGER) DESC LIMIT 1",(registration,)).fetchone() if info.get('phase')=='complete' else None)
    if identity_vin:
        if row:
            detail=json.loads(row[0])
            if detail.get('registered_car_vin')!=identity_vin:
                return None
        return lookup_vin(identity_vin,path)
    if not row:return None
    detail=json.loads(row[0]);vin=str(detail.get('registered_car_vin') or '')
    if not valid_vin(vin) or vin!=detail.get('vin'):return None
    known=_local_identity(vin,path)
    if known and plate(known.get('registration'))!=registration:
        return None
    return lookup_vin(vin,path)
