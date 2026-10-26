"""Persistent, server-side 17:00 reminders and enrolment in pre-reception."""
import asyncio
from datetime import datetime, timedelta, date
import json
from pathlib import Path
import sqlite3
from contextlib import closing
from zoneinfo import ZoneInfo

from pre_reception import db, get, save, phone

ROMANIA = ZoneInfo('Europe/Bucharest')
LOCK = asyncio.Lock()


def settings(root):
    path = Path(root)/'pre_arrival_reminders.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(enabled=False, start_date='', send_time='17:00')


def configure(root, enabled, start_date):
    date.fromisoformat(start_date)
    value=dict(enabled=bool(enabled),start_date=start_date,send_time='17:00')
    write_settings(root,value)
    return value


def write_settings(root,value):
    path=Path(root)/'pre_arrival_reminders.json'
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value),encoding='utf-8');temporary.replace(path)


def due(root, now):
    now=now.astimezone(ROMANIA)
    config=settings(root)
    return (config.get('enabled') and bool(config.get('start_date'))
            and now.date().isoformat()>=config['start_date'] and '17:00'<=now.strftime('%H:%M')<'18:00')


def appointments(db_path, target):
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory=sqlite3.Row
        rows=conn.execute("""SELECT a.*,r.phone AS request_phone,r.vin AS request_vin FROM scheduler_appointments a
          LEFT JOIN service_requests r ON r.id=a.request_id
          WHERE substr(a.start_at,1,10)=? AND a.status='programat'
          AND a.continued_from_id IS NULL AND (r.id IS NULL OR r.status NOT IN ('rezolvat','anulat','finalizat'))
          ORDER BY a.start_at,a.id""",(target.isoformat(),)).fetchall()
    result={}
    for row in rows:
        row=dict(row)
        original=str(row.get('dsm_original_start_at') or row['start_at'])
        if original[:10]!=target.isoformat():
            continue  # A continued/multi-day repair is already in the workshop.
        key=row.get('dsm_appointment_id') or 'local:'+str(row['id'])
        result.setdefault(key,row)
    return list(result.values())


def invitation(data):
    start=datetime.fromisoformat(data['appointment_start'])
    return (f'Bună ziua, {data.get("client_name") or "stimate client"}!\n'
        f'Vă reamintim programarea la ALBATROS A pentru mașina {data["registration"]}, '
        f'mâine, {start:%d.%m.%Y}, la ora {start:%H:%M}.\n\n'
        'Vă rugăm să confirmați venirea răspunzând DA sau NU.\n\n'
        'Pentru pregătirea comenzii și reducerea timpului de așteptare, trimiteți fotografii clare ale talonului, '
        'cu seria de caroserie și rubrica ITP vizibile. Dacă sunt pe fețe diferite, fotografiați ambele fețe.\n\n'
        'Vom verifica împreună datele mașinii, datele de contact și eventualele cerințe suplimentare pentru lucrare. '
        'Datele și cerințele vor fi reverificate și confirmate la semnarea comenzii. Mulțumim!')


def outbox(root):
    with db(root) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS reminders(
          reminder_key TEXT PRIMARY KEY, appointment_id INTEGER, start_at TEXT, phone TEXT,
          state TEXT NOT NULL, message_id TEXT, error TEXT, updated TEXT NOT NULL)''')


def status(root):
    outbox(root)
    with db(root) as conn:
        counts={r['state']:r['n'] for r in conn.execute('SELECT state,COUNT(*) n FROM reminders GROUP BY state')}
        errors=[dict(r) for r in conn.execute("SELECT appointment_id,start_at,state,error FROM reminders WHERE state IN ('sending','failed','uncertain','review') ORDER BY updated DESC LIMIT 20")]
        cases=[]
        for case in conn.execute('SELECT phone,stage,data FROM cases'):
            data=json.loads(case['data'])
            if data.get('production'):
                cases.append(dict(appointment_id=data.get('appointment_id'),registration=data.get('registration'),
                    phone_suffix=case['phone'][-4:],stage=case['stage'],command_code=(data.get('order') or {}).get('code')))
            if data.get('production') and (data.get('attendance_declined') or case['stage']=='review'):
                errors.append(dict(appointment_id=data.get('appointment_id'),start_at=data.get('appointment_start'),
                    state=case['stage'],error='Clientul nu confirmă venirea; verifică reprogramarea.' if data.get('attendance_declined') else 'Datele sau comanda necesită verificarea consilierului.'))
    return {**settings(root), 'timezone':'Europe/Bucharest','counts':counts,'issues':errors,'cases':cases}


def current_appointments(db_path,target,keys=None):
    return [a for a in appointments(db_path,target)
            if keys is None or not a.get('dsm_appointment_id') or str(a['dsm_appointment_id']) in keys]


async def preview(db_path, root, target, seed, keys=None):
    results=[]
    phones=set()
    for appointment in current_appointments(db_path,target,keys):
        try:
            data=await seed(appointment)
            if data['phone'] in phones:
                raise ValueError('Același număr are mai multe programări; verificare consilier pentru a nu amesteca mașinile.')
            phones.add(data['phone'])
            results.append(dict(appointment_id=appointment['id'],registration=appointment['registration'],eligible=True,phone_suffix=data['phone'][-4:]))
        except Exception as exc:
            results.append(dict(appointment_id=appointment['id'],registration=appointment['registration'],eligible=False,reason=str(exc)[:250]))
    return dict(date=target.isoformat(),candidates=results,eligible=sum(r['eligible'] for r in results))


async def tick(db_path, root, now, notify, seed, refresh=None, allowed=lambda:True):
    if not due(root,now) or LOCK.locked() or not allowed():
        return None
    async with LOCK:
        outbox(root)
        target=now.astimezone(ROMANIA).date()+timedelta(days=1)
        today=now.astimezone(ROMANIA).date().isoformat()
        if refresh and settings(root).get('last_refresh_day')!=today:
            try:
                snapshot=await refresh(target)
                config=settings(root);config.update(last_refresh_day=today,last_refresh_error='')
                config['last_refresh_ids']=snapshot.get('appointment_ids') if isinstance(snapshot,dict) else None
                write_settings(root,config)
            except Exception as exc:
                config=settings(root);config['last_refresh_error']=str(exc)[:300];write_settings(root,config)
                return dict(sent=0,skipped=0,failed=1)
        result=dict(sent=0,skipped=0,failed=0)
        for appointment in current_appointments(db_path,target,settings(root).get('last_refresh_ids')):
            key=f'{appointment.get("dsm_appointment_id") or appointment["id"]}:{target}'
            with db(root) as conn:
                if conn.execute('SELECT 1 FROM reminders WHERE reminder_key=?',(key,)).fetchone():
                    result['skipped']+=1;continue
            try:
                data=await seed(appointment)
                with db(root) as conn:
                    prior=conn.execute('SELECT data FROM cases WHERE phone=?',(data['phone'],)).fetchone()
                if prior and json.loads(prior['data']).get('production_optout'):
                    raise ValueError('Clientul a cerut oprirea mesajelor automate.')
                current=get(root,data['phone'])
                if current and current[1].get('production_optout'):
                    raise ValueError('Clientul a cerut oprirea mesajelor automate.')
                if current and current[0] not in {'done','stopped','invite_failed'}:
                    raise ValueError('Există deja o pre-recepție activă pentru acest număr; verificare consilier necesară.')
                # Recheck cancellation/rescheduling after slow provider reads.
                if not any(a['id']==appointment['id'] and a['start_at']==appointment['start_at'] for a in appointments(db_path,target)):
                    raise ValueError('Programarea s-a modificat înainte de trimitere.')
                if not due(root,now) or not allowed():
                    break
                data.update(production=True,appointment_id=appointment['id'],appointment_start=appointment['start_at'],
                            offer_id='arrival:'+key,expires_at=(target+timedelta(days=2)).isoformat())
                with db(root) as conn:
                    claimed=conn.execute('INSERT OR IGNORE INTO reminders VALUES (?,?,?,?,?,?,?,?)',
                        (key,appointment['id'],appointment['start_at'],data['phone'],'sending','','',now.isoformat())).rowcount
                if not claimed:
                    result['skipped']+=1;continue
                save(root,data['phone'],'attendance',data)
                try:
                    response=await notify(data['phone'],invitation(data),kind='request',
                        subject=f'confirmarea programării {data["registration"]} din {target:%d.%m.%Y}',
                        recipient_name=data.get('client_name') or 'client')
                    message_id=str(((response.get('messages') or [{}])[0]).get('id') or '')
                    with db(root) as conn:conn.execute("UPDATE reminders SET state='sent',message_id=?,updated=? WHERE reminder_key=?",(message_id,now.isoformat(),key))
                    result['sent']+=1
                except Exception as exc:
                    # A timeout may follow acceptance by Meta. Never resend an
                    # uncertain notification automatically after restart.
                    with db(root) as conn:conn.execute("UPDATE reminders SET state='uncertain',error=?,updated=? WHERE reminder_key=?",(str(exc)[:300],now.isoformat(),key))
                    result['failed']+=1
            except Exception as exc:
                with db(root) as conn:conn.execute('INSERT OR IGNORE INTO reminders VALUES (?,?,?,?,?,?,?,?)',
                    (key,appointment['id'],appointment['start_at'],'','review','',str(exc)[:300],now.isoformat()))
                result['failed']+=1
        return result
