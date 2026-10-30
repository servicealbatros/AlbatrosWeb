"""Brake-pad axle clarification; no assumed front axle or automatic offering."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import unicodedata

LABELS = {'front': 'față', 'rear': 'spate', 'both': 'față și spate'}
PADS = re.compile(r'\b(?:placut\w*|pastil\w*)(?:\s+(?:de\s+)?fran\w*)?\b')
OTHER = r'\b(?:discur\w*|amortiz\w*|bascul\w*|etrier\w*|bar[ae]|arip\w*)\b'
PREFIX = r'(?:(?:pe|puntea|punte|din|de|in|pentru|doar|numai)\s+)*'
AXES = r'(fata|spate)\b(?:(?:\s*(?:si|/|,)\s*|\s+)' + PREFIX + r'(fata|spate)\b)?'


def norm(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value).casefold())
                   if not unicodedata.combining(c))


def pad_axles(value):
    text = norm(value)
    axes = set()
    shared = re.search(r'\b(?:placut\w*\s+(?:si\s+)?discur\w*|discur\w*\s+(?:si\s+)?placut\w*)\s+(?:(?:de\s+)?frana\s+)?' + PREFIX + AXES, text)
    if shared and not re.match(r'\s*(?:sau|ori)\b', text[shared.end():]):
        axes.update(x for x in shared.groups() if x)
    for match in PADS.finditer(text):
        tail = re.split(r'[.;\n]|' + OTHER, text[match.end():], maxsplit=1)[0][:40]
        if re.match(r'\s*' + PREFIX + r'(?:ambele|amandoua)\b', tail):
            axes.update(('fata', 'spate'))
        prefix = re.match(r'\s*' + PREFIX + AXES, tail)
        if prefix and not re.match(r'\s*(?:sau|ori)\b', tail[prefix.end():]):
            axes.update(x for x in prefix.groups() if x)
    return axes


def needs_clarification(value):
    return bool(PADS.search(norm(value))) and not pad_axles(value)


def reply_axle(value):
    text = norm(value).strip(' .!?')
    if re.search(r'\b(?:nu|poate|probabil|stiu|cred)\b', text):
        return None
    if PADS.search(text):
        axes = pad_axles(text)
    elif re.fullmatch(r'(?:da|doar|pe|puntea|punte|puntile|punti|ambele|amandoua|toate|fata|spate|si|in|din|de|\s|/|,)+', text):
        if re.search(r'\b(?:ambele|amandoua)\b', text):
            return 'both'
        axes = set(re.findall(r'\b(?:fata|spate)\b', text))
    else:
        return None
    return 'both' if axes == {'fata', 'spate'} else 'front' if axes == {'fata'} else 'rear' if axes == {'spate'} else None


def complete(value, axle):
    if axle not in LABELS or not needs_clarification(value):
        raise ValueError('Puntea plăcuțelor trebuie verificată înainte de completare.')
    # Match on the original text too, preserving other requested work and km.
    return re.sub(r'\b(?:pl[ăa]cu[țt]\w*|pastil\w*)(?:\s+(?:de\s+)?fr[âa]n\w*)?\b',
                  lambda m: m[0] + ' ' + LABELS[axle], value, flags=re.I)


def phone(value):
    digits = re.sub(r'\D', '', str(value or ''))
    return '4' + digits if len(digits) == 10 and digits.startswith('07') else digits


def question(registration):
    plate = re.sub(r'[^A-Z0-9]', '', str(registration).upper())
    return f'Pentru schimbul plăcuțelor de frână la {plate}, doriți înlocuirea pe față, pe spate sau pe ambele punți?'


def schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS brake_scope_messages(
      request_id INTEGER PRIMARY KEY, phone TEXT NOT NULL, case_id TEXT NOT NULL,
      registration TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
      message_id TEXT, delivery_status TEXT)''')


def validate(conn, request_id):
    row = conn.execute('SELECT * FROM service_requests WHERE id=?', (request_id,)).fetchone()
    if not row or str(row['status']).lower() not in {'nou', 'in lucru', 'în lucru'}:
        raise ValueError('Cererea nu este activă pentru clarificarea lucrării.')
    row = dict(row)
    target = phone(row.get('phone'))
    if not re.fullmatch(r'407\d{8}', target) or not row.get('session_id') or not row.get('registration'):
        raise ValueError('Telefonul, conversația sau mașina cererii nu sunt confirmate.')
    for other in conn.execute('SELECT id,phone FROM service_requests WHERE id>?', (request_id,)):
        if phone(other['phone']) == target:
            raise ValueError('Există o cerere mai nouă pentru acest client; verifică asocierea conversației.')
    return row


def public_record(row):
    return {k: row[k] for k in ('request_id', 'state', 'message_id', 'delivery_status')}


async def ask(db_path, request_id, send, window_open):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        schema(conn)
        row = validate(conn, request_id)
        prior = conn.execute('SELECT * FROM brake_scope_messages WHERE request_id=?', (request_id,)).fetchone()
        if prior:
            return {**public_record(prior), 'already_requested': True}
        if not needs_clarification(row.get('symptom') or ''):
            raise ValueError('Cererea nu necesită clarificarea punții plăcuțelor.')
        target = phone(row['phone'])
        if not window_open(target):
            raise ValueError('Conversația WhatsApp nu este în fereastra de 24 de ore; nu trimit mesaj liber.')
        conflict = conn.execute("SELECT 1 FROM brake_scope_messages WHERE phone=? AND state IN ('sending','submitted','uncertain')", (target,)).fetchone()
        if conflict:
            raise ValueError('Există deja o clarificare în așteptare pentru acest număr.')
        conn.execute('INSERT INTO brake_scope_messages VALUES(?,?,?,?,?,?,?,?)',
                     (request_id, target, row['session_id'], row['registration'], 'sending',
                      datetime.now(timezone.utc).isoformat(), '', ''))
    # The claim survives restart, including an unknown HTTP outcome after send.
    try:
        response = await send(target, question(row['registration']), row['session_id'])
        ident = str(((response.get('messages') or [{}])[0]).get('id') or '')
        delivery = str(response.get('delivery_status') or 'accepted_by_meta') if ident else 'unknown'
        state = ('failed' if delivery == 'failed' else 'submitted') if ident else 'uncertain'
    except Exception:
        ident, state, delivery = '', 'uncertain', 'unknown'
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE brake_scope_messages SET state=CASE WHEN state='answered' THEN state ELSE ? END,message_id=?,delivery_status=? WHERE request_id=?",
                     (state, ident, delivery, request_id))
    return {**record(db_path, request_id), 'already_requested': False}


def record(db_path, request_id):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        schema(conn)
        row = conn.execute('SELECT * FROM brake_scope_messages WHERE request_id=?', (request_id,)).fetchone()
    return public_record(row) if row else None


def apply_reply(db_path, request_id, sender, case_id, registration, axle):
    """Update the exact existing request, preserving its VIN and other work."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        row = validate(conn, request_id)
        if (phone(row['phone']) != phone(sender) or row['session_id'] != case_id
                or str(row['registration']).upper() != str(registration).upper()):
            raise ValueError('Răspunsul nu aparține cererii și mașinii curente.')
        schema(conn)
        pending = conn.execute('SELECT * FROM brake_scope_messages WHERE request_id=?', (request_id,)).fetchone()
        if pending and (pending['case_id'] != case_id or pending['phone'] != phone(sender)
                        or datetime.now(timezone.utc) - datetime.fromisoformat(pending['created_at']) > timedelta(hours=24)):
            raise ValueError('Clarificarea a expirat sau nu mai aparține conversației curente.')
        if pending and pending['state'] == 'answered':
            expected = {'fata'} if axle == 'front' else {'spate'} if axle == 'rear' else {'fata', 'spate'}
            if pad_axles(row.get('symptom') or '') == expected:
                return row['symptom']
            raise ValueError('Clarificarea a fost deja completată; verificare consilier necesară.')
        symptom = complete(row.get('symptom') or '', axle)
        operations = json.loads(row.get('requested_operations') or '[]')
        if not isinstance(operations, list):
            raise ValueError('Operațiile existente trebuie verificate.')
        operations = [complete(op, axle) if isinstance(op, str) and needs_clarification(op) else op for op in operations]
        conn.execute('''UPDATE service_requests SET symptom=?,requested_operations=?,technical_labour_time=NULL,
          albatros_expected_time=NULL,booking_time=NULL,labour_source=NULL,labour_operation=NULL WHERE id=?''',
                     (symptom, json.dumps(operations, ensure_ascii=False), request_id))
        conn.execute("UPDATE brake_scope_messages SET state='answered' WHERE request_id=?", (request_id,))
    return symptom
