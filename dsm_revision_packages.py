"""Revision packages backed by DSM documents; never supplier orders."""
import argparse
import csv
import io
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEFAULT_DB = BASE / 'data' / 'dsm_revision_packages.sqlite3'


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value or '').lower()) if not unicodedata.combining(c))


def plate(value):
    return re.sub('[^A-Z0-9]', '', str(value or '').upper())


def category(row):
    text = normalized(row.get('name'))
    if re.search(r'cutie|transmis|gearbox|transmission|haldex|diferential', text): return 'other'
    if re.search(r'filtr\w*.*ulei|oil\s*filter', text): return 'oil_filter'
    if re.search(r'filtr\w*.*(combust|motorin|benzina)|fuel\s*filter', text): return 'fuel_filter'
    if re.search(r'filtr\w*.*(habitacl|polen|cabina|interior)|cabin\s*filter', text): return 'cabin_filter'
    if re.search(r'filtr\w*.*aer|air\s*filter', text): return 'air_filter'
    if re.search(r'buson.*ulei|surub.*golire.*ulei|(?:garnitur|saib|inel).*?(?:buson|filtr\w*.*ulei|baie.*ulei)',text):return 'oil_consumable'
    if not re.search(r'filtr|cutie|transmis|servo|diferential|frana|aditiv', text) and (re.search(r'ulei.*motor|motor.*ulei|engine oil|\b(?:0|5|10|15|20)\s*w\s*[-/]?\s*\d{2}\b', text)):
        return 'engine_oil'
    return 'other'


def revision_candidate(detail):
    text = normalized(' '.join([detail.get('description') or ''] + [r.get('name') or '' for r in detail.get('labors') or []]))
    has_oil_filter=any(category(r)=='oil_filter' for r in detail.get('parts') or [])
    if not has_oil_filter and re.search(r'revizi\w*\s+(?:la\s+)?(?:cutie|transmis)|schimb.*ulei.*(?:cutie|transmis)',text):return False
    return bool(re.search(r'revizi|schimb.*ulei|ulei.*filtr|filtr.*ulei|oil.*change', text) or has_oil_filter)


def amount(value):
    try:
        n = Decimal(str(value).replace(',', '.'))
        return n if n.is_finite() else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def validate_package(pkg):
    """A replacement labor line requires its corresponding filter SKU on this document."""
    categories = {category(p) for p in pkg.get('parts', []) if amount(p.get('quantity')) > 0}
    labels = {'fuel_filter': 'filtrului de combustibil', 'air_filter': 'filtrului de aer', 'cabin_filter': 'filtrului de habitaclu'}
    issues = set(pkg.get('issues') or [])
    for labor in pkg.get('labors') or []:
        text = normalized(labor.get('name'))
        if amount(labor.get('quantity')) <= 0 or not re.search(r'\b(?:inlocu\w*|schimb\w*|replac\w*)\b', text):
            continue
        if re.search(r'\bfara\s+(?:inlocu|schimb)|\bnu\s+(?:se\s+)?(?:inlocu|schimb)', text):
            continue
        expected = category(labor)
        if expected in labels and expected not in categories:
            issues.add(f'Manopera indică înlocuirea {labels[expected]}, dar piesa și codul nu apar pe acest deviz. Pachet incomplet: necesită verificare în DSM.')
    return {**pkg, 'issues': sorted(issues), 'ready': bool(pkg.get('ready')) and not issues}


def revalidate_catalogue(path=DEFAULT_DB):
    """Update old imports with current checks without changing source parts or invoices."""
    changed = 0
    with connect(path) as db:
        rows = db.execute('SELECT id,payload_json FROM packages').fetchall()
        for row in rows:
            original = json.loads(row['payload_json'])
            checked = validate_package(original)
            if checked != original:
                db.execute('UPDATE packages SET ready=?,payload_json=? WHERE id=?', (checked['ready'], json.dumps(checked, ensure_ascii=False), row['id']))
                changed += 1
        summary_row = db.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()
        stats = json.loads(summary_row[0]) if summary_row else {}
        stats.update(ready_packages=db.execute('SELECT count(*) FROM packages WHERE ready=1').fetchone()[0], completeness_check_at=datetime.now(timezone.utc).isoformat())
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('summary',?)", (json.dumps(stats, ensure_ascii=False),))
    return {'changed_packages': changed, 'ready_packages': stats['ready_packages']}


def package_parts(rows):
    """Keep original codes/brands/units; only aggregate exact same identity."""
    grouped = {}
    for r in rows:
        q = amount(r.get('quantity'))
        if q <= 0: continue
        key = (str(r.get('code') or '').strip(), str(r.get('manufacturer') or '').strip(), str(r.get('unit') or '').strip(), str(r.get('name') or '').strip())
        item = grouped.setdefault(key, {'code': key[0], 'manufacturer': key[1], 'unit': key[2], 'name': key[3], 'quantity': Decimal(0), 'category': category(r)})
        item['quantity'] += q
    return [{**x, 'quantity': float(x['quantity'])} for x in grouped.values()]


def invoice_evidence(record, document_types=('2',)):
    info = record.get('invoice_info') or {}
    if record.get('status') != 'billed' or str(record.get('document_type')) not in document_types: return []
    if str(info.get('transaction_head__type_2') or '') != 'F': return []
    found = []
    for inv in record.get('invoices') or []:
        if str(inv.get('transaction_id')) != str(info.get('invoice_head__id')): continue
        if str(inv.get('status')) != '3': continue
        found.append({'id': str(inv['transaction_id']), 'code': inv.get('code') or '', 'date': inv.get('transaction__datetime') or '', 'url': inv.get('url') or ''})
    return found


@contextmanager
def connect(path=DEFAULT_DB):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30); db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS packages(id TEXT PRIMARY KEY,registration TEXT,vin TEXT,vehicle TEXT,document_code TEXT,service_date TEXT,status TEXT,invoice_confirmed INTEGER,has_offer INTEGER,ready INTEGER,payload_json TEXT);
      CREATE INDEX IF NOT EXISTS package_plate ON packages(registration,service_date);
      CREATE INDEX IF NOT EXISTS package_vin ON packages(vin,service_date);
      CREATE TABLE IF NOT EXISTS offers(id TEXT PRIMARY KEY,payload_json TEXT);
      CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS vehicle_vins(registration TEXT,vin TEXT,PRIMARY KEY(registration,vin));
    ''')
    try:
        with db: yield db
    finally: db.close()


def ingest(records, path=DEFAULT_DB, metadata=None):
    # Rebuild the audited view atomically; failed/partial audits are never final totals.
    docs = {str(r['transaction_id']): r for r in records}
    children_to_offers = {}
    for ident, offer in docs.items():
        if str(offer.get('document_type')) == '1':
            for child in offer.get('children') or []:
                child_id = str(child.get('transaction_head__id') or child.get('transaction_id') or child.get('id') or '')
                children_to_offers.setdefault(child_id,set()).add(ident)
    def source_offers(ident):
        found=set();seen=set();pending=[ident]
        while pending:
            current=pending.pop()
            if current in seen: continue
            seen.add(current);found.update(children_to_offers.get(current,()))
            doc=docs.get(current) or {}
            explicit=str(doc.get('source_offer_id') or (doc.get('invoice_info') or {}).get('transaction_head_offer__id') or '')
            if explicit in docs and str(docs[explicit].get('document_type'))=='1':found.add(explicit)
            parent=str(doc.get('parent_id') or '')
            found.update(children_to_offers.get(parent,()))
            if parent in docs:
                if str(docs[parent].get('document_type'))=='1':found.add(parent)
                else:pending.append(parent)
        return found
    packages = []; offers = []
    for ident, d in docs.items():
        if not revision_candidate(d): continue
        if str(d.get('document_type')) == '1':
            offers.append(d); continue
        if str(d.get('document_type')) != '2': continue
        parts = package_parts(d.get('parts') or [])
        if not parts: continue
        invoices = invoice_evidence(d)
        linked = source_offers(ident)
        cats = {p['category'] for p in parts}
        notes = []
        if not invoices: notes.append('Factura nu este confirmată prin documentul fiscal DSM.')
        if 'oil_filter' not in cats: notes.append('Filtrul de ulei nu este identificat.')
        if 'engine_oil' not in cats: notes.append('Uleiul de motor nu este identificat.')
        if not any(amount(r.get('quantity'))>0 for r in d.get('labors') or []): notes.append('Manopera cu o cantitate pozitivă lipsește.')
        if any(amount(r.get('quantity')) < 0 for r in d.get('parts') or []): notes.append('Devizul conține retururi/corecții; verifică piesele nete înainte de reutilizare.')
        if not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', str(d.get('vin') or '')): notes.append('VIN incomplet sau nevalid.')
        revision_parts = [p for p in parts if p['category'] != 'other']
        sku_names={}
        for p in revision_parts:
            sku_names.setdefault((p['code'],p['manufacturer'],p['unit']),set()).add(p['name'])
            if not p['code'] or not p['manufacturer']: notes.append('Există o piesă fără cod sau marcă.')
            if not p['unit']: notes.append('Există o piesă fără unitate de măsură.')
            if p['unit'].lower() not in {'l','lt','litru','litri','liter','litre','liters','litres'} and not p['quantity'].is_integer():
                notes.append('Cantitate fracționară în unități de ambalaj; verifică ambalajul înainte de coș.')
        if any(len(names)>1 for names in sku_names.values()):notes.append('Același cod și aceeași marcă au descrieri diferite; verifică identitatea produsului.')
        pkg = {k: d.get(k) for k in ('transaction_id','document_code','registration','vin','vehicle','service_date','description','status')}
        pkg.update(registration=plate(d.get('registration')), parts=revision_parts, other_parts=[p for p in parts if p['category']=='other'], labors=d.get('labors') or [], invoices=invoices, offer_ids=sorted(linked), offers=[{'id':i,'code':docs[i].get('document_code') or i,'status':docs[i].get('status')} for i in sorted(linked)], issues=sorted(set(notes)), ready=not notes, customer_type='unknown', checked_at=d.get('checked_at'))
        packages.append(validate_package(pkg))
    confirmed_offers = set()
    for p in packages:
        if p['invoices']:
            for ident in p['offer_ids']:
                if ident in docs and revision_candidate(docs[ident]): confirmed_offers.add(ident)
    stats = {'revision_offers':len(offers),'offers_with_invoice':len(confirmed_offers),'offers_without_confirmed_invoice':len(offers)-len(confirmed_offers),'canceled_revision_offers':sum(o.get('status')=='canceled' for o in offers),'revision_orders':len(packages),'invoiced_revision_orders':sum(bool(p['invoices']) for p in packages),'unlinked_invoiced_revision_orders':sum(bool(p['invoices']) and not p['offer_ids'] for p in packages),'ready_packages':sum(p['ready'] for p in packages),'checked_at':datetime.now(timezone.utc).isoformat(),**(metadata or {})}
    with connect(path) as db:
        db.execute('DELETE FROM packages'); db.execute('DELETE FROM offers'); db.execute('DELETE FROM vehicle_vins')
        for d in docs.values():
            if d.get('registration') and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',str(d.get('vin') or '')):
                db.execute('INSERT OR IGNORE INTO vehicle_vins VALUES (?,?)',(plate(d['registration']),d['vin']))
        for p in packages:
            db.execute('INSERT INTO packages VALUES (?,?,?,?,?,?,?,?,?,?,?)', (str(p['transaction_id']),p['registration'],p.get('vin'),p.get('vehicle'),p.get('document_code'),p.get('service_date'),p.get('status'),bool(p['invoices']),bool(p['offer_ids']),p['ready'],json.dumps(p,ensure_ascii=False)))
        for o in offers: db.execute('INSERT INTO offers VALUES (?,?)',(str(o['transaction_id']),json.dumps(o,ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('summary',json.dumps(stats,ensure_ascii=False)))
    return stats


def summary(path=DEFAULT_DB):
    with connect(path) as db:
        r=db.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()
        return json.loads(r[0]) if r else {'complete':False,'message':'Auditul nu a fost încă importat.'}


def search(registration='', vin='', path=DEFAULT_DB):
    registration=plate(registration);vin=str(vin or '').strip().upper()
    if not registration and not vin: return {'packages':[],'vins':[]}
    with connect(path) as db:
        rows=db.execute('SELECT payload_json FROM packages WHERE '+('vin=?' if vin else 'registration=?')+' ORDER BY service_date DESC,CAST(id AS INTEGER) DESC',(vin or registration,)).fetchall()
        known_vins=[r[0] for r in db.execute('SELECT vin FROM vehicle_vins WHERE registration=? ORDER BY vin',(registration,))] if registration else []
    packages=[validate_package(json.loads(x[0])) for x in rows]
    vins=sorted(set(known_vins)|{p['vin'] for p in packages if re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',str(p.get('vin') or ''))})
    return {'packages':packages,'vins':vins,'ambiguous_registration':len(vins)>1 and not vin}


def export_text(pkg):
    return '\n'.join(f"{p['code']}\t{p['manufacturer']}\t{p['quantity']:g}\t{p['unit']}" for p in pkg['parts'])


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('audit');p.add_argument('--db',default=str(DEFAULT_DB));args=p.parse_args()
    audit=json.loads(Path(args.audit).read_text(encoding='utf-8'))
    if not audit.get('summary',{}).get('complete'): raise SystemExit('Audit incomplet: nu poate înlocui baza verificată.')
    print(json.dumps(ingest(audit['records'],args.db,audit['summary']),ensure_ascii=False))
