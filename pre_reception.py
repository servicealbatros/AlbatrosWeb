"""Restart-safe pre-reception for the pilot and explicitly enrolled appointments."""
import asyncio
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
from pre_arrival_policy import can_reuse, load_stored_vehicle, preparation_ready, saved_date

RECIPIENTS = tuple(value.strip() for value in os.getenv('PRE_RECEPTION_TEST_PHONES', ('40700100000', '40700100006')[0] + ',' + ('40700100000', '40700100006')[1]).split(',') if value.strip())
LOCK = asyncio.Lock()


def phone(value):
    value = re.sub(r'\D', '', str(value))
    return '4' + value if len(value) == 10 and value.startswith('07') else value


def norm(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c)).strip(' .!')


def itp_date(value):
    match = re.fullmatch(r'\s*(\d{2})[./-](\d{2})[./-](\d{4})\s*', value)
    if not match:
        return None
    try:
        return date(int(match[3]), int(match[2]), int(match[1])).isoformat()
    except ValueError:
        return None


def document_summary(data):
    return (f'1/3 — Confirmăm mașina și titularul talonului.\nSeria de caroserie: {data["vin"]}\n'
            f'Nume titular: {data.get("owner_name") or "nu s-a citit clar"}\n'
            'Dacă sunt corecte, răspunde DA. Dacă numele lipsește sau este greșit, scrie numele corect. '
            'Pentru o problemă la serie, scrie VIN: urmat de seria exactă. Nu trimite CNP.')


def document_itp_question(data):
    known = data.get('itp')
    previous = data.get('previous_dsm', {}).get('car_itp_expires_date')
    comparison = f' În DSM era salvată data {previous}; verificăm valabilitatea actuală.' if previous and not data.get('production') else ''
    return ('2/3 — ' + (f'Data expirării ITP citită/notată pentru această recepție este {known}. Confirmi? Răspunde DA sau scrie data corectă în format ZZ.LL.AAAA.' if known else
            'Trimite și fotografia clară a rubricii ITP din talon. Dacă data nu se citește, scrie data actuală a expirării în format ZZ.LL.AAAA (nu data efectuării inspecției).') + comparison)


async def send_with_agreement(root, sender, data, send, message, stage, agreement):
    """One customer message, with a restart-safe native GDPR link when ready."""
    include_link = False
    if agreement and data.get('production') and data.get('document_source') == 'document':
        try:
            record = await agreement(data, lambda: save(root, sender, stage, data))
        except Exception:
            record = {**(data.get('gdpr') or {}), 'state': 'review', 'reason': 'dsm_unavailable'}
            data['gdpr'] = record
        if record.get('state') == 'pending' and record.get('delivery') not in {'submitted', 'sending', 'uncertain'}:
            from dsm_gdpr import signing_link
            try:
                link = signing_link(record.get('link'))
            except ValueError:
                record.update(state='review', reason='invalid_link')
            else:
                message += ('\n\nVă rugăm să citiți «Acord GDPR Client» din linkul securizat DSM:\n' + link +
                            '\nFormularul include acorduri opționale pentru marketing. '
                            'Alegeți personal opțiunile și semnați dacă sunteți de acord. '
                            'Pentru refuzul semnării răspundeți REFUZ GDPR; pregătirea comenzii poate continua. '
                            '\nDupă semnare, răspundeți AM SEMNAT. Vom verifica semnarea în sistem; '
                            'răspunsul din WhatsApp nu ține locul semnăturii.')
                record['delivery'] = 'sending'
                include_link = True
        if record.get('state') == 'awaiting_order':
            message += ('\nAcordul GDPR pentru semnare va fi trimis după confirmarea datelor și '
                        'pregătirea comenzii, când documentul este disponibil în sistem.')
        elif record.get('state') == 'review':
            message += '\nConsilierul va verifica acordul GDPR și linkul pentru semnare.'
        save(root, sender, stage, data)
    try:
        await send(sender, message)
    except Exception:
        if include_link:
            data['gdpr']['delivery'] = 'uncertain'
            save(root, sender, stage, data)
        raise
    if include_link:
        # Successful API submission is not proof of delivery or signature.
        data['gdpr']['delivery'] = 'submitted'
        save(root, sender, stage, data)


async def request_document(root, sender, data, send, *, agreement=None):
    # Confirmation from a previous reception must never satisfy this request.
    for key in ('document_verified', 'document_current', 'document_fields_confirmed',
                'identity_fields_confirmed', 'itp_confirmed', 'final_confirmed',
                'itp', 'itp_source', 'owner_name', 'manufacture_year',
                'photo_registration', 'document_difference'):
        data.pop(key, None)
    data['document_source'] = 'document'
    data['document_refresh_requested'] = True
    save(root, sender, 'document', data)
    context = (f'TEST ALBATROS A — reluăm verificarea pe comanda {data["retest_command_code"]}, fără o comandă duplicată.\n'
               if data.get('test_run_id') and data.get('retest_command_code') else '')
    if data.get('manual_test') and not context:
        context = f'TEST ALBATROS A — {data["registration"]}, fără rezervare în programator.\n'
    await send_with_agreement(root, sender, data, send,
        context + f'Vă rugăm să ne trimiteți o fotografie clară a talonului mașinii {data["registration"]}, pentru a ne ajuta să actualizăm datele și să scurtăm timpul de așteptare la intrarea mașinii în service. Vă rugăm să includeți seria de șasiu (VIN) și rubrica privind valabilitatea ITP. Dacă sunt pe fețe diferite, trimiteți fotografii cu ambele fețe. Nu este nevoie de CNP.',
        'document', agreement)


def document_year_question(data):
    known = data.get('manufacture_year')
    return ('3/3 — ' + (f'Anul fabricației citit/notat este {known}. Confirmi? Răspunde DA sau scrie anul corect.' if known else
            'Care este anul fabricației? Scrie anul cu patru cifre; nu presupunem că este anul primei înmatriculări.'))


def document_ready(data):
    return bool(data.get('document_verified') and data.get('owner_name') and data.get('manufacture_year') and data.get('itp'))


@contextmanager
def db(root):
    root = Path(root) / 'pre_reception_private'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / 'pilot.sqlite3'
    conn = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript('''CREATE TABLE IF NOT EXISTS cases(phone TEXT PRIMARY KEY, stage TEXT NOT NULL, data TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(offer TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, phone TEXT, stage TEXT, created TEXT);''')
        with conn:
            yield conn
    finally:
        conn.close()


def get(root, sender):
    with db(root) as conn:
        row = conn.execute('SELECT stage,data FROM cases WHERE phone=?', (phone(sender),)).fetchone()
    if not row:
        return None
    data=json.loads(row['data'])
    if data.get('production'):
        if data.get('expires_at','') < date.today().isoformat():
            return None
    elif phone(sender) not in RECIPIENTS:
        return None
    return row['stage'],data


def save(root, sender, stage, data):
    now = datetime.now(timezone.utc).isoformat()
    with db(root) as conn:
        conn.execute('INSERT OR REPLACE INTO cases VALUES(?,?,?,?)', (phone(sender), stage, json.dumps(data, ensure_ascii=False), now))
        conn.execute('INSERT INTO events(phone,stage,created) VALUES(?,?,?)', (phone(sender), stage, now))


def status(root):
    with db(root) as conn:
        rows = [r for r in conn.execute('SELECT phone,stage,updated,data FROM cases') if not json.loads(r['data']).get('production')]
        orders = conn.execute("SELECT state,result FROM orders WHERE offer NOT LIKE 'arrival:%'").fetchall()
    from pre_arrival_reminders import settings
    config=settings(root)
    return {'production_enabled': bool(config.get('enabled')), 'production_start_date': config.get('start_date'),
            'flow_version': 3, 'reminder_hour': '17:00', 'registration': 'B101TST',
            'cases': [{'phone_suffix': r['phone'][-4:], 'stage': r['stage'], 'updated': r['updated'],
                       'registration': json.loads(r['data']).get('registration')} for r in rows],
            'orders': [{'state': r['state'], 'result': json.loads(r['result']) if r['result'] else None} for r in orders]}


async def start(root, notify, seed):
    async with LOCK:
        existing = [get(root, p) for p in RECIPIENTS]
        if all(existing):
            return status(root)  # No duplicate invitation on HTTP retry.
        source = await seed()
        for target in RECIPIENTS:
            if get(root, target):
                continue
            save(root, target, 'attendance', source)
            try:
                await notify(target, 'TEST ALBATROS A — B101TST. Simulăm acum pregătirea pentru primirea în service, fără rezervare reală în programator. '
                             'După verificarea talonului și confirmarea finală a datelor vom deschide o comandă REALĂ de TEST în DSM, pentru anulare ulterioară. '
                             'Confirmi participarea la test? Răspunde DA sau NU.', kind='request', subject='test pregătire recepție B101TST', recipient_name='client')
            except Exception:
                save(root, target, 'invite_failed', source)
        return status(root)


async def restart(root, send, seed, *, run_id=None, recipient=None):
    """Replay only this pilot against its verified existing command."""
    async with LOCK:
        if run_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(run_id)):
            raise ValueError('Identificator de probă invalid.')
        if recipient is not None and phone(recipient) not in RECIPIENTS:
            raise ValueError('Proba este permisă numai pe numerele de test.')
        if recipient is not None and not run_id:
            raise ValueError('Reluarea individuală necesită un identificator de probă.')
        existing = [get(root, target) for target in RECIPIENTS]
        if run_id and any(case and case[1].get('test_run_id') == run_id for case in existing):
            return status(root)
        with db(root) as conn:
            creating = conn.execute("SELECT 1 FROM orders WHERE state='creating' LIMIT 1").fetchone()
        if creating or any(case and case[0] in {'creating', 'review'} for case in existing):
            raise ValueError('Verifică rezultatul comenzii în curs înainte de o probă nouă.')
        active = [case for case in existing if case and (case[1].get('retest_command_id') or case[1].get('retest_run'))
                  and case[0] not in {'done', 'stopped', 'invite_failed'}]
        if active and not run_id:
            return status(root)  # A repeated click cannot reset replies or resend.
        prior_new = next((case[1].get('order', {}).get('id') for case in existing if case and case[1].get('new_test_order') and case[1].get('order', {}).get('id')), None)
        data = await seed(retest=True, existing_command_id=prior_new) if prior_new else await seed(retest=True)
        if not data.get('retest_command_id') and not data.get('new_test_order'):
            raise ValueError('Reluarea testului necesită o comandă verificată.')
        data['retest_run'] = True
        if run_id:
            data['test_run_id'] = run_id
        with db(root) as conn:
            backup_path = Path(root) / 'pre_reception_private' / ('before-retest-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.sqlite3')
            with closing(sqlite3.connect(backup_path)) as backup:
                conn.backup(backup)
            os.chmod(backup_path, 0o600)
            conn.execute('DELETE FROM orders WHERE offer=?', (data['offer_id'],))
        for target in RECIPIENTS:
            if recipient is not None and target != phone(recipient):
                previous = get(root, target)
                if previous and previous[0] not in {'done', 'stopped'}:
                    save(root, target, 'stopped', previous[1])
                continue
            try:
                await request_document(root, target, dict(data), send)
            except Exception:
                save(root, target, 'invite_failed', dict(data))
        return status(root)


def summary(data):
    expiry = saved_date(data.get('itp'))
    expired = bool(expiry and expiry < date.today())
    if data.get('production'):
        reused = data.get('document_source') == 'dsm' and can_reuse(data)
        identity = (f'Date preluate din fișa mașinii; ultima vizită: {saved_date(data["document_policy"]["last_visit"]):%d.%m.%Y}.\n'
                    if reused else f'Titular talon: {data.get("owner_name","—")}\n')
        return (f'Verificați datele pentru pregătirea comenzii {data["registration"]}:\n'
            f'Serie de caroserie: {data["vin"]}\n' + identity +
            f'An fabricație: {data.get("manufacture_year") or "neînregistrat"}\nITP până la: {data.get("itp") or "neînregistrat"}' + (' — EXPIRAT' if expired else '') + '\n'
            f'Client: {data.get("client_name","—")}\nAdresă: {data.get("client_address","—")}\nTelefon: {data["phone"]}\n'
            f'Lucrări: {data["work"]}\nCerințe suplimentare: {data.get("extra","Nu")}\n'
            'Datele și cerințele vor fi reverificate și confirmate la semnarea comenzii, împreună cu consilierul. '
            'Nu se aprobă costuri suplimentare prin acest mesaj. Pentru pregătirea comenzii răspundeți CONFIRM DATELE. '
            'Pentru corecturi: CORECTEAZA CLIENT sau CORECTEAZA.')
    operation = f'actualizarea comenzii de TEST {data["retest_command_code"]}' if data.get('retest_command_id') else 'crearea comenzii REALE'
    return (f'TEST {data["registration"]} — verifică datele înainte de {operation}:\nVIN: {data["vin"]}\n'
            f'ITP până la: {data["itp"]} (' + ('declarat de client' if data.get('itp_source') == 'client' else 'citit din talon') + ')' + (' — EXPIRAT' if expired else '') + '\n'
            + (f'Înmatriculare în fotografia veche: {data["photo_registration"]}; înmatricularea curentă din ofertă: {data["registration"]}.\n' if data.get('photo_registration') else '') +
            f'Nume client: {data.get("client_name") or data.get("client", "—")}\nAdresă: {data.get("client_address", "—")}\nTelefon: {data["phone"]}\n'
            f'Titular talon: {data.get("owner_name", "—")}\nAn fabricație confirmat: {data.get("manufacture_year", "—")}\n'
            f'Lucrări din oferta semnată: {data["work"]}\nSuplimentar: {data["extra"]}\n'
            'Datele și cerințele clientului trebuie reverificate și confirmate la semnarea comenzii, împreună cu consilierul.\n'
            'Nu rezervăm un interval în programator. Data ITP confirmată se salvează în Date Mașină. Dacă totul este corect, răspunde exact CONFIRM DATELE. '
            'Pentru modificarea datelor clientului scrie CORECTEAZA CLIENT. Pentru corectarea talonului răspunde CORECTEAZA.')


async def request_missing_client(root, sender, data, send):
    if not data.get('client_name'):
        save(root, sender, 'client', data)
        await send(sender, 'Scrie doar numele complet al clientului sau denumirea firmei. Îți cer doar datele lipsă. Nu trimite CNP.')
    elif not data.get('client_address'):
        save(root, sender, 'client_address', data)
        await send(sender, 'Scrie adresa completă de facturare: localitate, județ/sector, stradă și număr.')
    elif not data.get('phone') or (data.get('production') and not data.get('phone_confirmed')):
        save(root, sender, 'phone', data)
        await send(sender, f'Confirmi numărul de contact +{phone(sender)}? Răspunde DA sau scrie numărul corect.')
    elif 'extra' not in data:
        save(root, sender, 'requirements', data)
        label='Lucrările programate' if data.get('production') else 'Lucrări din oferta semnată'
        await send(sender, f'{label}: {data["work"]}\nCe cerințe suplimentare ai? Scrie-le aici sau răspunde NU.')
    else:
        save(root, sender, 'confirm', data)
        await send(sender, summary(data))


async def text(root, sender, body, send, writer, *, refresh_policy=None, agreement=None):
    if not get(root, sender):
        return False
    async with LOCK:
        stage, data = get(root, sender)
        value = norm(body)
        if data.get('production') and value in {'stop','dezabonare'}:
            data['production_optout']=True
            save(root,sender,'stopped',data)
            await send(sender,'Am oprit mesajele automate pentru acest număr. Programarea existentă nu a fost anulată.')
            return True
        if value == 'stop test':
            save(root, sender, 'stopped', data)
            await send(sender, 'Testul este oprit pentru acest număr. Nu anulez comenzi sau programări existente.')
            return True
        if stage == 'stopped':
            return False
        if (agreement and data.get('production') and data.get('document_source') == 'document'
                and value in {'refuz gdpr', 'nu semnez gdpr', 'nu doresc sa semnez gdpr'}):
            record = data.get('gdpr') or {}
            already_signed = record.get('state') == 'signed'
            record.update(state='declined', declined_at=datetime.now(timezone.utc).isoformat())
            record.pop('link', None)
            record.pop('verified_at', None)
            record.pop('verification_source', None)
            if already_signed:
                record['reason'] = 'existing_agreement_requires_review'
            data['gdpr'] = record
            save(root, sender, stage, data)
            message = 'Am notat refuzul semnării în acest flux. Pregătirea comenzii poate continua.'
            if already_signed:
                message += ' Consilierul va verifica separat acordul existent în DSM.'
            if stage == 'document':
                message += ' Așteptăm fotografiile clare ale talonului, inclusiv rubrica ITP.'
            elif stage == 'confirm':
                message += ' Pentru pregătirea comenzii, răspundeți CONFIRM DATELE.'
            await send(sender, message)
            return True
        if (agreement and data.get('production') and data.get('document_source') == 'document'
                and value in {'am semnat', 'semnat', 'verifica gdpr'}):
            try:
                record = await agreement(data, lambda: save(root, sender, stage, data), check_only=True)
            except Exception:
                record = {**(data.get('gdpr') or {}), 'state': 'review', 'reason': 'dsm_unavailable'}
                data['gdpr'] = record
            save(root, sender, stage, data)
            message = ('Semnarea acordului GDPR este confirmată în DSM.' if record.get('state') == 'signed'
                       else 'Semnarea acordului GDPR nu este încă verificată în sistem. Nu am înregistrat răspunsul din WhatsApp ca semnătură.')
            if stage == 'document':
                message += ' Așteptăm fotografiile clare ale talonului, inclusiv rubrica ITP.'
            elif stage == 'confirm':
                message += ' Pentru pregătirea comenzii, răspundeți CONFIRM DATELE.'
            await send(sender, message)
            return True
        if stage == 'done':
            if data.get('production'):
                if value=='confirm datele':
                    await send(sender,f'Comanda {data["order"]["code"]} este deja pregătită. Nu se creează o comandă duplicată.')
                    return True
                return False
            await send(sender, f'Comanda de TEST {data["order"]["code"]} există deja. Nu creez o a doua comandă. Pentru încheierea conversației de test: STOP TEST.')
            return True
        policy_review = (data.get('document_policy') or {}).get('decision') not in {'reuse_dsm','request_document'}
        if (data.get('production') and not data.get('document_verified')
                and (stage in {'attendance','invite_failed'} and value in {'da', 'confirm', 'confirm participarea'}
                     or stage=='document' or stage=='review' and policy_review and value in {'da','confirm datele'})
                and refresh_policy):
            try:
                data['document_policy'] = await refresh_policy(data)
            except Exception:
                data['document_policy']={'decision':'review','reason':'history_unavailable'}
                save(root,sender,'review',data)
                await send(sender,'Nu am putut verifica ultima vizită în service. Consilierul va verifica datele existente înainte de a cere documente sau de a deschide comanda.')
                return True
        if (stage=='document' or stage=='review' and policy_review) and data.get('production'):
            if (data.get('document_policy') or {}).get('decision') not in {'reuse_dsm','request_document'}:
                save(root,sender,'review',data)
                await send(sender,'Consilierul va verifica data ultimei vizite și datele mașinii. Nu vă solicităm talonul până la clarificare.')
                return True
            if stage=='review' and not can_reuse(data):
                await request_document(root,sender,data,send,agreement=agreement)
                return True
        if (stage=='document' or stage=='review' and policy_review) and can_reuse(data):
            load_stored_vehicle(data)
            await request_missing_client(root,sender,data,send)
            return True
        if stage in {'attendance', 'invite_failed'}:
            if value in {'nu', 'nu confirm'}:
                if data.get('production'):
                    data['attendance_declined']=True
                save(root, sender, 'stopped', data)
                await send(sender, 'Am notat că nu confirmați venirea. Consilierul va verifica reprogramarea; programarea nu a fost anulată automat.' if data.get('production') else 'Am oprit simularea. Nu am anulat oferta sau vreo programare reală.')
            elif value in {'da', 'confirm', 'confirm participarea'}:
                if can_reuse(data):
                    load_stored_vehicle(data)
                    await request_missing_client(root,sender,data,send)
                elif data.get('production') and (data.get('document_policy') or {}).get('decision') not in {'reuse_dsm','request_document'}:
                    save(root,sender,'review',data)
                    await send(sender,'Consilierul va verifica data ultimei vizite și datele mașinii. Nu vă solicităm talonul până la clarificare.')
                else:
                    await request_document(root, sender, data, send, agreement=agreement)
            else:
                await send(sender, 'Pentru confirmarea programării răspundeți DA sau NU.' if data.get('production') else 'Pentru testul B101TST răspunde DA sau NU.')
        elif stage == 'document':
            await send(sender, f'Vă rugăm să ne trimiteți fotografiile clare ale talonului mașinii {data["registration"]}, inclusiv seria de șasiu (VIN) și rubrica privind valabilitatea ITP, pentru actualizarea datelor și scurtarea timpului de așteptare la intrarea în service.')
        elif stage == 'document_check':
            if value in {'da', 'confirm talonul'} and data.get('owner_name'):
                valid = True
            elif norm(body).startswith('vin:'):
                valid = re.sub(r'\W', '', body.partition(':')[2]).upper() == data['vin'] and bool(data.get('owner_name'))
            elif value not in {'da', 'nu', 'confirm talonul'} and 2 <= len(body.strip()) <= 180:
                data['owner_name'] = re.sub(r'^nume\s*:\s*', '', body.strip(), flags=re.I)
                valid = len(data['owner_name']) >= 2
            else:
                valid = False
            if not valid or not data.get('document_verified'):
                await send(sender, 'Nu am putut confirma seria și numele.\n' + document_summary(data))
            else:
                data['identity_fields_confirmed'] = True
                save(root, sender, 'document_itp', data)
                await send(sender, document_itp_question(data))
        elif stage in {'itp', 'document_itp'}:
            expiry = data.get('itp') if value == 'da' else itp_date(body)
            if not expiry or not data.get('document_verified'):
                await send(sender, document_itp_question(data))
            else:
                data['itp'] = expiry
                if value != 'da':
                    data['itp_source'] = 'client'
                data['itp_confirmed'] = True
                if not data.get('identity_fields_confirmed'):
                    save(root, sender, 'document_check', data)
                    await send(sender, document_summary(data))
                else:
                    save(root, sender, 'document_year', data)
                    await send(sender, document_year_question(data))
        elif stage == 'document_year':
            year = str(data.get('manufacture_year') or '') if value == 'da' else body.strip()
            if not re.fullmatch(r'\d{4}', year) or not 1886 <= int(year) <= date.today().year + 1:
                await send(sender, document_year_question(data))
            else:
                data['manufacture_year'] = int(year)
                if not document_ready(data) or not data.get('identity_fields_confirmed') or not data.get('itp_confirmed'):
                    save(root, sender, 'document_check', data)
                    await send(sender, document_summary(data))
                else:
                    data['document_fields_confirmed'] = True
                    save(root, sender, 'requirements', data)
                    label='Pentru programare avem' if data.get('production') else 'În oferta semnată avem'
                    await send(sender, f'Datele talonului sunt confirmate. {label}: {data["work"]}\nCe cerințe mai ai pentru lucrare? Scrie-le aici sau răspunde NU dacă nu ai completări. Datele și cerințele vor fi reverificate și confirmate la semnarea comenzii, fără acceptarea unor costuri noi prin acest mesaj.')
        elif stage == 'client':
            if value == 'da' and data.get('client_name'):
                save(root, sender, 'client_address', data)
                await send(sender, f'Adresa salvată: {data.get("client_address") or "lipsește"}. Răspunde DA dacă este corectă sau scrie adresa completă corectă.')
            elif len(body.strip()) < 2 or len(body) > 200 or ',' in body or '\n' in body or value in {'da', 'nu'}:
                await send(sender, 'Scrie doar numele complet al clientului sau denumirea firmei. Îți cer separat adresa și telefonul. Nu trimite CNP.')
            else:
                data['client_name'] = body.strip()
                data['client'] = body.strip()
                if data.get('editing_client'):
                    save(root, sender, 'client_address', data)
                    await send(sender, f'Adresa salvată: {data.get("client_address") or "lipsește"}. Răspunde DA dacă este corectă sau scrie adresa completă corectă.')
                else:
                    await request_missing_client(root, sender, data, send)
        elif stage == 'client_address':
            address = data.get('client_address', '') if value == 'da' else body.strip()
            if not 8 <= len(address) <= 600:
                await send(sender, 'Scrie adresa completă de facturare, între 8 și 600 de caractere.')
            else:
                data['client_address'] = address
                if data.get('editing_client'):
                    save(root, sender, 'phone', data)
                    await send(sender, f'Confirmi numărul de contact +{data.get("phone") or phone(sender)}? Răspunde DA sau scrie numărul corect.')
                else:
                    await request_missing_client(root, sender, data, send)
        elif stage == 'phone':
            target = phone(data.get('phone') or sender) if value == 'da' else phone(body)
            if not re.fullmatch(r'407\d{8}', target):
                await send(sender, 'Scrie un număr mobil românesc valid sau DA pentru numărul acestei conversații.')
            else:
                data['phone'] = target
                data['phone_confirmed'] = True
                data.pop('editing_client', None)
                await request_missing_client(root, sender, data, send)
        elif stage == 'requirements':
            if len(body) > 1800:
                await send(sender, 'Trimite cerințele în cel mult 1.800 de caractere.')
            else:
                data['extra'] = 'Nu' if value == 'nu' else body.strip()
                await request_missing_client(root, sender, data, send)
        elif stage in {'confirm', 'review', 'creating'}:
            if value == 'corecteaza client':
                data.pop('final_confirmed', None)
                data['editing_client'] = True
                save(root, sender, 'client', data)
                await send(sender, f'Numele salvat: {data.get("client_name") or "lipsește"}. Răspunde DA dacă este corect sau scrie numele corect. Verificăm apoi adresa și telefonul.')
            elif value == 'corecteaza':
                await request_document(root, sender, data, send, agreement=agreement)
            elif value == 'confirm datele':
                if not preparation_ready(data):
                    if data.get('production') and (data.get('document_policy') or {}).get('decision') not in {'reuse_dsm','request_document'}:
                        save(root,sender,'review',data)
                        await send(sender,'Data ultimei vizite trebuie verificată înaintea pregătirii comenzii. Nu solicităm încă talonul.')
                        return True
                    await request_document(root, sender, data, send, agreement=agreement)
                    return True
                if not data.get('client_name') or not data.get('client_address'):
                    data.pop('final_confirmed', None)
                    await request_missing_client(root, sender, data, send)
                    return True
                data['final_confirmed'] = True
                save(root, sender, 'creating', data)
                with db(root) as conn:
                    conn.execute('INSERT OR IGNORE INTO orders(offer,state) VALUES(?,?)', (data['offer_id'], 'creating'))
                    allow_create = conn.execute('SELECT changes()').fetchone()[0] == 1
                    prior = conn.execute('SELECT state,result FROM orders WHERE offer=?', (data['offer_id'],)).fetchone()
                try:
                    if prior['state'] != 'done' and prior['result']:
                        data['_created_command_id'] = json.loads(prior['result']).get('id')
                    result = json.loads(prior['result']) if prior['state'] == 'done' else await writer(data, allow_create)
                    if result.get('itp') and result['itp'] != data.get('itp'):
                        raise ValueError('Un alt participant a confirmat deja o dată ITP diferită; verificare consilier necesară.')
                    with db(root) as conn:
                        conn.execute('UPDATE orders SET state=?,result=? WHERE offer=?', ('done', json.dumps(result), data['offer_id']))
                    data['order'] = result
                    save(root, sender, 'done', data)
                    await send_with_agreement(root, sender, data, send,
                        f'Comanda {result["code"]} pentru {data["registration"]} este pregătită. Vă așteptăm la programarea confirmată. Datele și cerințele vor fi reverificate la semnarea comenzii.' if data.get('production') else f'Comanda de TEST {result["code"]} a fost verificată în DSM pentru {data["registration"]}. Data expirării ITP {data["itp"]} a fost salvată și verificată în Date Mașină. Nu am făcut rezervare în programator.',
                        'done', agreement)
                except Exception:
                    save(root, sender, 'review', data)
                    await send(sender, 'Crearea sau verificarea comenzii necesită intervenția consilierului. Nu repet automat crearea și nu declar comanda finalizată.')
            else:
                await send(sender, 'Pentru pregătirea comenzii răspundeți CONFIRM DATELE sau CORECTEAZA.' if data.get('production') else 'Pentru crearea comenzii reale de TEST răspunde CONFIRM DATELE sau CORECTEAZA.')
        else:
            await send(sender, 'Verificarea comenzii este în curs. Nu se creează o comandă suplimentară.')
    return True


async def image(root, sender, message, send, download, reader):
    current = get(root, sender)
    if not current or current[0] == 'stopped':
        return False
    async with LOCK:
        stage, data = get(root, sender)
        if data.get('production') and stage=='attendance':
            await send(sender,'Vă rugăm să confirmați mai întâi venirea răspunzând DA sau NU, apoi să trimiteți fotografiile talonului.')
            return True
        if stage not in {'document', 'itp', 'document_check', 'document_itp', 'document_year'}:
            await send(sender, 'Fotografia nu modifică datele deja verificate. Pentru reluarea verificării răspunde CORECTEAZA la confirmarea finală.')
            return True
        media_id = (message.get('image') or {}).get('id')
        if not media_id:
            await send(sender, 'Retrimite fotografia talonului, te rog.')
            return True
        try:
            content, mime = await download(media_id)
            if mime not in {'image/jpeg', 'image/png', 'image/webp'} or len(content) > 15000000:
                raise ValueError('Tip sau dimensiune imagine neacceptată')
            # Preserve the received document before OCR, including rejected reads,
            # so an adviser can distinguish a wrong photo from an OCR mistake.
            directory = Path(root) / 'pre_reception_private' / 'documents'
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination = directory / (hashlib.sha256((phone(sender) + media_id).encode()).hexdigest() + '.img')
            destination.write_bytes(content)
            os.chmod(destination, 0o600)
            parsed = await asyncio.to_thread(reader, content, mime)
            vin = re.sub(r'\W', '', str(parsed.get('vin') or '')).upper()
            plate = re.sub(r'\W', '', str(parsed.get('registration') or '')).upper()
            evidence = {'sender': phone(sender), 'image': str(destination), 'mime': mime,
                        'parsed': parsed, 'received': datetime.now(timezone.utc).isoformat()}
            evidence_path = destination.with_suffix('.json')
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding='utf-8')
            os.chmod(evidence_path, 0o600)
            differences = []
            reference='programare' if data.get('production') else 'oferta semnată'
            if (data.get('production') and data.get('vehicle_missing') and stage=='document' and not data.get('document_verified')
                    and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',vin) and plate==data['registration']):
                data['vin']=vin
            if vin and vin != data['vin']:
                differences.append(f'VIN citit: {vin}; VIN din {reference}: {data["vin"]}')
            if plate and plate != data['registration'] and vin != data['vin']:
                differences.append(f'înmatriculare citită: {plate}; în {reference}: {data["registration"]}')
            if differences:
                data['document_difference'] = differences
                for key in ('document_verified', 'document_current', 'document_fields_confirmed',
                            'identity_fields_confirmed', 'itp_confirmed', 'itp', 'itp_source'):
                    data.pop(key, None)
                save(root, sender, 'document', data)
                await send(sender, 'Verificarea fotografiei a găsit: ' + '; '.join(differences) + '. Poate fi o eroare de citire. Vechimea pozei nu este motivul blocării. Trimite o fotografie mai clară pentru verificare; nu deschid încă o comandă.')
                return True
            data.pop('document_difference', None)
            if vin == data['vin'] and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',vin):
                data['document_verified'] = True
                data['document_current'] = True
                if plate and plate != data['registration']:
                    # VIN identifies the car. A historical plate is disclosed in
                    # the final summary, never used to overwrite today's plate.
                    data['photo_registration'] = plate
                else:
                    data.pop('photo_registration', None)
            if data.get('document_verified'):
                owner = str(parsed.get('owner_name') or '').strip()
                year = str(parsed.get('manufacture_year') or '')
                if 2 <= len(owner) <= 180:
                    data['owner_name'] = owner
                if re.fullmatch(r'\d{4}', year) and 1886 <= int(year) <= date.today().year + 1:
                    data['manufacture_year'] = int(year)
            # A reverse-side photo without identity is accepted only after matching front.
            itp = str(parsed.get('itp_expires') or '')
            if data.get('document_verified') and parsed.get('itp_evidence') and re.fullmatch(r'\d{4}-\d{2}-\d{2}', itp):
                date.fromisoformat(itp)
                data['itp'] = itp
                data['itp_source'] = 'document'
            if data.get('document_verified'):
                for key in ('document_fields_confirmed', 'identity_fields_confirmed', 'itp_confirmed'):
                    data.pop(key, None)
                save(root, sender, 'document_check', data)
                await send(sender, document_summary(data))
            else:
                save(root, sender, 'document', data)
                await send(sender, 'Mai am nevoie de fotografia clară cu VIN și înmatriculare.')
        except Exception:
            await send(sender, 'Nu am putut verifica fotografia. Trimite o imagine mai clară; nu am creat comanda.')
    return True
