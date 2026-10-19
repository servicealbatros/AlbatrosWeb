"""Restart-safe, opt-in pilot. No production reminder scheduler is enabled here."""
import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata

RECIPIENTS = ('40700100000', '40700100006')
LOCK = asyncio.Lock()


def phone(value):
    value = re.sub(r'\D', '', str(value))
    return '4' + value if len(value) == 10 and value.startswith('07') else value


def norm(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c)).strip(' .!')


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
    if phone(sender) not in RECIPIENTS:
        return None
    with db(root) as conn:
        row = conn.execute('SELECT stage,data FROM cases WHERE phone=?', (phone(sender),)).fetchone()
    return (row['stage'], json.loads(row['data'])) if row else None


def save(root, sender, stage, data):
    now = datetime.now(timezone.utc).isoformat()
    with db(root) as conn:
        conn.execute('INSERT OR REPLACE INTO cases VALUES(?,?,?,?)', (phone(sender), stage, json.dumps(data, ensure_ascii=False), now))
        conn.execute('INSERT INTO events(phone,stage,created) VALUES(?,?,?)', (phone(sender), stage, now))


def status(root):
    with db(root) as conn:
        rows = conn.execute('SELECT phone,stage,updated FROM cases').fetchall()
        orders = conn.execute('SELECT state,result FROM orders').fetchall()
    return {'production_enabled': False, 'reminder_hour': '17:00', 'registration': 'B101TST',
            'cases': [{'phone_suffix': r['phone'][-4:], 'stage': r['stage'], 'updated': r['updated']} for r in rows],
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


def summary(data):
    expired = date.fromisoformat(data['itp']) < date.today()
    return (f'TEST B101TST — verifică datele înainte de crearea comenzii REALE:\nVIN: {data["vin"]}\n'
            f'ITP până la: {data["itp"]}' + (' — EXPIRAT' if expired else '') + '\n'
            + (f'Înmatriculare în fotografia veche: {data["photo_registration"]}; înmatricularea curentă din ofertă: {data["registration"]}.\n' if data.get('photo_registration') else '') +
            f'Beneficiar și adresă: {data["client"]}\nTelefon: {data["phone"]}\n'
            f'Lucrări din oferta semnată: {data["work"]}\nSuplimentar: {data["extra"]}\n'
            'Nu rezervăm un interval în programator. Dacă totul este corect și accepți crearea comenzii reale de TEST, răspunde exact CONFIRM DATELE. '
            'Pentru corectări răspunde CORECTEAZA.')


async def text(root, sender, body, send, writer):
    if not get(root, sender):
        return False
    async with LOCK:
        stage, data = get(root, sender)
        value = norm(body)
        if value == 'stop test':
            save(root, sender, 'stopped', data)
            await send(sender, 'Testul este oprit pentru acest număr. Nu anulez comenzi sau programări existente.')
            return True
        if stage == 'stopped':
            return False
        if stage == 'done':
            await send(sender, f'Comanda de TEST {data["order"]["code"]} există deja. Nu creez o a doua comandă. Pentru încheierea conversației de test: STOP TEST.')
            return True
        if stage in {'attendance', 'invite_failed'}:
            if value in {'nu', 'nu confirm'}:
                save(root, sender, 'stopped', data)
                await send(sender, 'Am oprit simularea. Nu am anulat oferta sau vreo programare reală.')
            elif value in {'da', 'confirm', 'confirm participarea'}:
                save(root, sender, 'document', data)
                await send(sender, 'Trimite, te rog, fotografii clare ale talonului B101TST, inclusiv partea cu VIN și partea cu valabilitatea ITP. Nu este nevoie de CNP.')
            else:
                await send(sender, 'Pentru testul B101TST răspunde DA sau NU.')
        elif stage == 'document':
            await send(sender, 'Aștept fotografiile talonului B101TST și ale rubricii ITP pentru verificare. Textul nu înlocuiește verificarea documentului în acest test.')
        elif stage == 'client':
            if len(body.strip()) < 8 or len(body) > 600:
                await send(sender, 'Scrie numele beneficiarului și adresa completă de facturare; pentru firmă, denumire și CUI. Nu trimite CNP.')
            else:
                data['client'] = body.strip()
                save(root, sender, 'phone', data)
                await send(sender, f'Confirmi numărul de contact +{phone(sender)}? Răspunde DA sau scrie numărul corect.')
        elif stage == 'phone':
            target = phone(sender) if value == 'da' else phone(body)
            if not re.fullmatch(r'407\d{8}', target):
                await send(sender, 'Scrie un număr mobil românesc valid sau DA pentru numărul acestei conversații.')
            else:
                data['phone'] = target
                save(root, sender, 'requirements', data)
                await send(sender, f'Lucrări din oferta semnată: {data["work"]}\nMai ai cerințe suplimentare? Scrie-le aici sau răspunde NU. Acestea se notează pentru verificare, fără acceptarea unor costuri noi.')
        elif stage == 'requirements':
            if len(body) > 1800:
                await send(sender, 'Trimite cerințele în cel mult 1.800 de caractere.')
            else:
                data['extra'] = 'Nu' if value == 'nu' else body.strip()
                save(root, sender, 'confirm', data)
                await send(sender, summary(data))
        elif stage in {'confirm', 'review'}:
            if value == 'corecteaza':
                data.pop('final_confirmed', None)
                save(root, sender, 'document', data)
                await send(sender, 'Reluăm verificarea: trimite talonul și rubrica ITP, apoi îți cer din nou datele de contact și cerințele.')
            elif value == 'confirm datele':
                data['final_confirmed'] = True
                save(root, sender, 'creating', data)
                with db(root) as conn:
                    conn.execute('INSERT OR IGNORE INTO orders(offer,state) VALUES(?,?)', (data['offer_id'], 'creating'))
                    allow_create = conn.execute('SELECT changes()').fetchone()[0] == 1
                    prior = conn.execute('SELECT state,result FROM orders WHERE offer=?', (data['offer_id'],)).fetchone()
                try:
                    result = json.loads(prior['result']) if prior['state'] == 'done' else await writer(data, allow_create)
                    with db(root) as conn:
                        conn.execute('UPDATE orders SET state=?,result=? WHERE offer=?', ('done', json.dumps(result), data['offer_id']))
                    data['order'] = result
                    save(root, sender, 'done', data)
                    await send(sender, f'Comanda REALĂ de TEST {result["code"]} a fost verificată în DSM pentru B101TST. Poate fi identificată pentru anulare ulterioară. Nu am făcut rezervare în programator.')
                except Exception:
                    save(root, sender, 'review', data)
                    await send(sender, 'Crearea sau verificarea comenzii necesită intervenția consilierului. Nu repet automat crearea și nu declar comanda finalizată.')
            else:
                await send(sender, 'Pentru crearea comenzii reale de TEST răspunde CONFIRM DATELE sau CORECTEAZA.')
        else:
            await send(sender, 'Verificarea comenzii este în curs. Nu se creează o comandă suplimentară.')
    return True


async def image(root, sender, message, send, download, reader):
    current = get(root, sender)
    if not current or current[0] == 'stopped':
        return False
    async with LOCK:
        stage, data = get(root, sender)
        if stage != 'document':
            await send(sender, 'Fotografia nu modifică datele deja verificate. Pentru reluarea verificării răspunde CORECTEAZA la confirmarea finală.')
            return True
        media_id = (message.get('image') or {}).get('id')
        if not media_id:
            await send(sender, 'Retrimite fotografia talonului, te rog.')
            return True
        await send(sender, 'Verific fotografia pentru testul B101TST.')
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
            if vin and vin != data['vin']:
                differences.append(f'VIN citit: {vin}; VIN din oferta semnată: {data["vin"]}')
            if plate and plate != data['registration'] and vin != data['vin']:
                differences.append(f'înmatriculare citită: {plate}; în oferta semnată: {data["registration"]}')
            if differences:
                data['document_difference'] = differences
                save(root, sender, 'document', data)
                await send(sender, 'Verificarea fotografiei a găsit: ' + '; '.join(differences) + '. Poate fi o eroare de citire. Vechimea pozei nu este motivul blocării. Trimite o fotografie mai clară pentru verificare; nu deschid încă o comandă.')
                return True
            data.pop('document_difference', None)
            if vin == data['vin']:
                data['document_verified'] = True
                if plate and plate != data['registration']:
                    # VIN identifies the car. A historical plate is disclosed in
                    # the final summary, never used to overwrite today's plate.
                    data['photo_registration'] = plate
                else:
                    data.pop('photo_registration', None)
            # A reverse-side photo without identity is accepted only after matching front.
            itp = str(parsed.get('itp_expires') or '')
            if data.get('document_verified') and parsed.get('itp_evidence') and re.fullmatch(r'\d{4}-\d{2}-\d{2}', itp):
                date.fromisoformat(itp)
                data['itp'] = itp
            if data.get('document_verified') and data.get('itp'):
                save(root, sender, 'client', data)
                await send(sender, 'Am verificat VIN-ul și înmatricularea. Scrie numele beneficiarului și adresa completă de facturare (pentru firmă: denumire, CUI și adresă). Beneficiarul poate fi diferit de proprietarul din talon. Nu trimite CNP.')
            else:
                save(root, sender, 'document', data)
                await send(sender, 'Mai am nevoie de ' + ('fotografia clară cu VIN și înmatriculare.' if not data.get('document_verified') else 'fotografia rubricii cu data expirării ITP. Nu folosesc data primei înmatriculări drept ITP.'))
        except Exception:
            await send(sender, 'Nu am putut verifica fotografia. Trimite o imagine mai clară; nu am creat comanda.')
    return True
