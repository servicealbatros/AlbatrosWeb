"""Explicit advisor work orders through the existing server-owned DSM session.

This never sends WhatsApp messages, approves an offer, or invents talon fields.
"""
import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from dsm_pre_reception import call
from dsm_readonly import _search_transactions
from dsm_billing_sync import _read_details
from dsm_open_orders_readonly import _evaluate

LOCK = asyncio.Lock()


class AdvisorOrderInput(BaseModel):
    operation_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{12,80}$')
    source_id: str = Field(pattern=r'^[0-9]{2,12}$')
    car_id: str = Field(pattern=r'^[0-9]{2,12}$')
    vin: str = Field(pattern=r'^[A-HJ-NPR-Z0-9]{17}$')
    phone: str = Field(pattern=r'^407[0-9]{8}$')
    previous_registration: str = Field(pattern=r'^[A-Z0-9]{5,10}$')
    registration: str = Field(pattern=r'^[A-Z0-9]{5,10}$')
    work: str = Field(min_length=5, max_length=1000)
    dry_run: bool = True


def plate(value):
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def phone(value):
    digits = re.sub(r'\D', '', str(value or ''))
    return '4' + digits if len(digits) == 10 and digits.startswith('07') else digits


def check_identity(head, data, contact_id=None, order_id=None):
    if (str(head.get('car_id')) != data.car_id
            or str(head.get('car_body_series') or '').upper() != data.vin
            or contact_id is not None and str(head.get('contact_id')) != contact_id
            or order_id is not None and (str(head.get('transaction_head__id')) != order_id
                or str(head.get('transaction_head__type')) != '2'
                or head.get('transaction_head__status') != 'working')):
        raise ValueError('Identitatea sau starea documentului DSM nu corespunde.')


async def inspect(data):
    source = await call('/special/transaction/get_transaction/' + data.source_id)
    head = source.get('transaction') or {}
    check_identity(head, data)
    contact_id = str(head.get('contact_id') or '')
    if not contact_id.isdigit() or int(contact_id) <= 1:
        raise ValueError('Clientul DSM nu este identificat.')
    car = await call('/special/car/get_car', {'car_id': data.car_id})
    if (str(car.get('car_body_series') or '').upper() != data.vin
            or plate(car.get('car_auto_number')) not in {data.previous_registration, data.registration}):
        raise ValueError('VIN-ul sau numărul actual din DSM s-a modificat; reverificare necesară.')
    client = await call('/special/contact/get_client', {'contact_id': contact_id, 'transaction_id': data.source_id})
    if not any(phone(m.get('val')) == data.phone for m in client.get('mobile') or []):
        raise ValueError('Telefonul solicitării nu corespunde clientului DSM.')
    # Check both registrations in live DSM, including the current spelling.
    refs = {}
    for registration in sorted({data.previous_registration, data.registration}):
        for ref in await _search_transactions(registration):
            if str(ref.get('transaction_type')) == '2' and ref.get('status') == 'working':
                refs[str(ref['transaction_id'])] = ref
    details = await _read_details(list(refs)) if refs else {}
    orders = []
    for ident in refs:
        h = (details.get(ident) or {}).get('transaction') or {}
        check_identity(h, data, contact_id, ident)
        orders.append({'id': ident, 'code': str(h.get('transaction_head__code') or ident)})
    return contact_id, orders


def journal(data_dir):
    folder = Path(data_dir) / 'dsm_advisor_orders'
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / 'operations.sqlite3')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, order_id TEXT, code TEXT)')
    return conn


async def update_plate(data, ident):
    # A fixed DSM route and fixed field: no arbitrary browser/script input.
    payload = {'car_id': data.car_id, 'transaction_id': ident,
               'input': 'car_auto_number', 'val': data.registration}
    result = await _evaluate('''(async()=>{
      if(location.protocol!=='https:'||!['s.servicemanager.q-service.ro','apps.dsmpro.ro','appm.dsmpro.ro'].includes(location.hostname))throw Error('Domeniu DSM nepermis');
      const r=await fetch('/special/car/update_car_detail',{method:'POST',credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/x-www-form-urlencoded','X-CSRF-TOKEN':document.querySelector('meta[name="csrf-token"]')?.content||''},body:new URLSearchParams(__DATA__),signal:AbortSignal.timeout(12000)});
      if(!r.ok||r.url.includes('/login'))throw Error('DSM indisponibil');return JSON.stringify(await r.json());})()'''.replace('__DATA__', json.dumps(payload)))
    if str(result.get('status')) not in {'1', 'true', 'True'} or result.get('validation'):
        raise ValueError('DSM nu a confirmat schimbarea numărului; comanda este păstrată pentru verificare.')
    car = await call('/special/car/get_car', {'car_id': data.car_id})
    if str(car.get('car_body_series') or '').upper() != data.vin or plate(car.get('car_auto_number')) != data.registration:
        raise ValueError('Numărul salvat nu este confirmat în fișa mașinii DSM.')


async def run(data_dir, data):
    async with LOCK:
        contact_id, orders = await inspect(data)
        if data.dry_run:
            return {'ready': not orders, 'registration': data.registration,
                    'previous_registration': data.previous_registration, 'existing_orders': orders,
                    'identity_verified': True, 'work': data.work, 'dry_run': True}
        fingerprint = hashlib.sha256(data.model_dump_json(exclude={'dry_run'}).encode()).hexdigest()
        conn = journal(data_dir)
        try:
            row = conn.execute('SELECT * FROM operations WHERE operation_id=?', (data.operation_id,)).fetchone()
            if row and row['fingerprint'] != fingerprint:
                raise ValueError('Identificatorul operației a fost folosit pentru alte date.')
            ident = str(row['order_id'] or '') if row else ''
            if row and not ident:
                raise ValueError('Crearea anterioară este incertă; nu repet operația DSM.')
            if orders and (not ident or len(orders) != 1 or orders[0]['id'] != ident):
                return {'created': False, 'existing_orders': orders, 'review_required': True}
            if not ident:
                # Commit intent BEFORE DSM writes. An ambiguous response is never retried.
                conn.execute('INSERT INTO operations(operation_id,fingerprint,state) VALUES (?,?,?)',
                             (data.operation_id, fingerprint, 'creating'))
                conn.commit()
                moment = datetime.now(ZoneInfo('Europe/Bucharest')).strftime('%Y-%m-%d %H:%M')
                result = await call('/special/transaction/start_transaction', {
                    'contact_id': contact_id, 'car_id': data.car_id, 'transaction_type': '2',
                    'change_insurance': '0', 'reset_client': '0', 'reset_car': '0',
                    'recalculate_row': '0', 'reset_center': '0', 'transaction_start': moment,
                    'transaction_duration': '0:00', 'transaction_agreed_period': moment})
                ident = str(result.get('transaction_id') or '')
                if not ident.isdigit() or int(ident) <= 1 or ident == data.source_id:
                    raise ValueError('DSM nu a confirmat ID-ul comenzii; nu repet crearea.')
                conn.execute('UPDATE operations SET order_id=?,state=? WHERE operation_id=?',
                             (ident, 'created', data.operation_id))
                conn.commit()
            order = await call('/special/transaction/get_transaction/' + ident)
            head = order.get('transaction') or {}
            if not head.get('car_id'):
                if (str(head.get('transaction_head__id')) != ident
                        or str(head.get('transaction_head__type')) != '2'
                        or head.get('transaction_head__status') != 'working'
                        or str(head.get('contact_id')) != contact_id or order.get('transaction_rows')):
                    raise ValueError('Comandă incompletă neconcordantă; verificare necesară.')
                await call('/special/transaction/transaction_car', {'car_id': data.car_id,
                           'contact_id': contact_id, 'transaction_id': ident})
                head = (await call('/special/transaction/get_transaction/' + ident)).get('transaction') or {}
            check_identity(head, data, contact_id, ident)
            car = await call('/special/car/get_car', {'car_id': data.car_id})
            if str(car.get('car_body_series') or '').upper() != data.vin:
                raise ValueError('VIN modificat înaintea salvării; verificare necesară.')
            if plate(car.get('car_auto_number')) != data.registration:
                if plate(car.get('car_auto_number')) != data.previous_registration:
                    raise ValueError('Numărul mașinii s-a modificat între timp.')
                await update_plate(data, ident)
            description = str(head.get('transaction_head__description') or '')
            if data.work not in description:
                desired = '\n'.join(x for x in [description.strip(), data.work.strip()] if x)
                result = await call('/special/transaction/update_transaction_details', {
                    'transaction_id': ident, 'car_id': data.car_id, 'transaction_head__description': desired})
                if str(result.get('status')) not in {'1', 'true', 'True'}:
                    raise ValueError('Cerința nu a fost confirmată; comanda există, nu repet crearea.')
            check = (await call('/special/transaction/get_transaction/' + ident)).get('transaction') or {}
            check_identity(check, data, contact_id, ident)
            if plate(check.get('car_auto_number')) != data.registration or data.work not in str(check.get('transaction_head__description') or ''):
                raise ValueError('Numărul sau lucrarea nu este confirmată pe comandă; nu repet crearea.')
            code = str(check.get('transaction_head__code') or '')
            if not code:
                raise ValueError('Codul real al comenzii nu a fost returnat de DSM.')
            conn.execute('UPDATE operations SET state=?,code=? WHERE operation_id=?', ('done', code, data.operation_id))
            conn.commit()
            return {'created': True, 'id': ident, 'code': code,
                    'registration': data.registration, 'verified': True}
        finally:
            conn.close()
