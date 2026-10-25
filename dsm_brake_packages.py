"""Separate invoiced brake replacements by component using explicit labor evidence."""
import csv
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dsm_revision_packages import normalized, plate, amount, invoice_evidence

BASE=Path(__file__).resolve().parent
DEFAULT_DB=BASE/'data'/'dsm_brake_packages.sqlite3'
LABELS={'pads':'Plăcuțe de frână','shoes':'Saboți de frână','discs':'Discuri de frână','drums':'Tamburi de frână'}


def component_categories(name, labor=False):
    t=normalized(name)
    if re.search(r'inmatric|lumina|ilumin|\bbec\b|ambreiaj|volanta|slefu|abraziv',t):return set()
    target=re.sub(r'^(?:manopera\s+)?(?:inlocu\w*|schimb\w*|replac\w*|renew\w*)\s+','',t)
    target=re.sub(r'^(?:ambilor|ambelor|tuturor|toate|ambele|ambii|toti)\s+','',target)
    if re.match(r'^reglaj',target) and (not labor or target!=t):return set()
    auxiliary=r'^(?:(?:set|kit)\s+(?:de\s+)?(?:accesori|arc|reparat|protect|suport|montaj|montare|pargh|reglaj|fixar|intretin)|senzor|contact|indicator|surub|piulit|arc\w*\b|pargh|cablu|aparatoare|protect|preotect|taler|tabla|scut|suport|etrier|cilindr)'
    if re.search(auxiliary,target):return set()
    if labor and re.search(r'^(?:butuc|punte|rulment|instalati)',target):return set()
    result=set()
    if re.search(r'\bplacut\w*|brake\s*pads?\b|bremsbelag',t):result.add('pads')
    if re.search(r'\bsabot\w*|brake\s*shoes?\b|bremsback|garnitur\w*.*fran.*tambur',t):result.add('shoes')
    if re.search(r'\bdisc(?:uri\w*|ul\w*)?\b',t) and (re.search(r'fran|brak',t) or (labor and re.search(r'\bdiscuri\w*\b',t))):result.add('discs')
    if re.search(r'bremsscheib|brake\s*(?:disc|rotor)',t):result.add('discs')
    if re.search(r'\btambur\w*|brake\s*drum|bremstrommel',t):result.add('drums')
    if not labor:
        if 'pads' in result:result.discard('discs')
        if 'shoes' in result:result.discard('drums')
    return result


def replacement_labor(row):
    cats=component_categories(row.get('name'),labor=True)
    if amount(row.get('quantity'))<=0:return set(),set()
    text=normalized(row.get('name'))
    action=bool(re.search(r'\b(?:inlocu\w*|schimb\w*|replac\w*|renew\w*)\b',text))
    negated=bool(re.search(r'\bfara\s+(?:inlocu|schimb)|\bnu\s+(?:se\s+)?(?:inlocu|schimb|necesita)',text))
    return (cats if action and not negated else set()),cats


def aggregate_parts(rows):
    grouped={}
    for r in rows:
        cats=component_categories(r.get('name'))
        if not cats or amount(r.get('quantity'))<=0:continue
        for cat in cats:
            key=(cat,str(r.get('code') or '').strip(),str(r.get('manufacturer') or '').strip(),str(r.get('unit') or '').strip(),str(r.get('name') or '').strip())
            item=grouped.setdefault(key,{'category':cat,'code':key[1],'manufacturer':key[2],'unit':key[3],'name':key[4],'quantity':amount(0)})
            item['quantity']+=amount(r.get('quantity'))
    return [{**r,'quantity':float(r['quantity'])} for r in grouped.values()]


@contextmanager
def connect(path=DEFAULT_DB):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p,timeout=30);db.row_factory=sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,registration TEXT,vin TEXT,service_date TEXT,payload_json TEXT);
    CREATE INDEX IF NOT EXISTS brake_plate ON jobs(registration,service_date);
    CREATE TABLE IF NOT EXISTS components(job_id TEXT,category TEXT,status TEXT,PRIMARY KEY(job_id,category));
    CREATE TABLE IF NOT EXISTS vehicle_vins(registration TEXT,vin TEXT,PRIMARY KEY(registration,vin));
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);''')
    try:
        with db:yield db
    finally:db.close()


def ingest(records,path=DEFAULT_DB,metadata=None):
    records=list(records);jobs=[]
    confirmed={c:set() for c in LABELS};with_parts={c:set() for c in LABELS};labor_only={c:set() for c in LABELS};review={c:set() for c in LABELS}
    all_confirmed=set();all_review=set();ready=0
    for d in records:
        invoices=invoice_evidence(d,document_types=('2','4'))
        if not invoices:continue
        parts=aggregate_parts(d.get('parts') or [])
        pcats={p['category'] for p in parts};labor_cats=set();labor_mentions=set();labor_by_cat={c:[] for c in LABELS}
        for l in d.get('labors') or []:
            actual,mentioned=replacement_labor(l);labor_cats.update(actual);labor_mentions.update(mentioned)
            for cat in actual:labor_by_cat[cat].append(l)
        categories=pcats|labor_cats|labor_mentions
        if not categories:continue
        invoice_ids={str(i['id']) for i in invoices};components=[]
        for cat in LABELS:
            if cat not in categories:continue
            cp=[p for p in parts if p['category']==cat]
            status='with_parts' if cat in labor_cats and cp else 'labor_only' if cat in labor_cats else 'review'
            issues=[]
            if status=='review':issues.append('Înlocuirea nu este confirmată printr-o manoperă explicită pe deviz.')
            if not cp:issues.append('Piesele acestei lucrări nu apar pe același deviz.')
            if not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',str(d.get('vin') or '')):issues.append('VIN incomplet sau nevalid.')
            if any(not p['code'] or not p['manufacturer'] or not p['unit'] for p in cp):issues.append('Există o piesă fără cod, marcă sau unitate.')
            if any(amount(r.get('quantity'))<0 and cat in component_categories(r.get('name')) for r in d.get('parts') or []):issues.append('Există retururi/corecții de cantitate pentru această categorie.')
            if any(not p['quantity'].is_integer() for p in cp):issues.append('Cantitate fracționară; verifică unitatea de vânzare.')
            is_ready=status=='with_parts' and not issues;ready+=int(is_ready)
            components.append({'category':cat,'label':LABELS[cat],'status':status,'parts':cp,'labors':labor_by_cat[cat],'ready':is_ready,'issues':issues})
            if status=='review':review[cat].update(invoice_ids);all_review.update(invoice_ids)
            else:
                confirmed[cat].update(invoice_ids);all_confirmed.update(invoice_ids)
                (with_parts if status=='with_parts' else labor_only)[cat].update(invoice_ids)
        job={k:d.get(k) for k in ('transaction_id','document_code','document_type','registration','vin','vehicle','service_date','description','checked_at')}
        job.update(registration=plate(d.get('registration')),invoices=invoices,components=components)
        jobs.append(job)
    stats={'confirmed_invoices':len(all_confirmed),'review_invoices':len(all_review),'jobs':len(jobs),'ready_components':ready,'categories':{c:{'label':LABELS[c],'confirmed_invoices':len(confirmed[c]),'with_parts':len(with_parts[c]),'labor_only':len(labor_only[c]-with_parts[c]),'review_invoices':len(review[c])} for c in LABELS},'checked_at':datetime.now(timezone.utc).isoformat(),**(metadata or {})}
    with connect(path) as db:
        db.execute('DELETE FROM jobs');db.execute('DELETE FROM components');db.execute('DELETE FROM vehicle_vins')
        for d in records:
            if d.get('registration') and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',str(d.get('vin') or '')):
                db.execute('INSERT OR IGNORE INTO vehicle_vins VALUES (?,?)',(plate(d['registration']),d['vin']))
        for j in jobs:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(str(j['transaction_id']),j['registration'],j.get('vin'),j.get('service_date'),json.dumps(j,ensure_ascii=False)))
            for c in j['components']:db.execute('INSERT INTO components VALUES (?,?,?)',(str(j['transaction_id']),c['category'],c['status']))
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('summary',json.dumps(stats,ensure_ascii=False)))
    return stats


def summary(path=DEFAULT_DB):
    with connect(path) as db:r=db.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()
    return json.loads(r[0]) if r else {'complete':False,'categories':{}}


def search(registration='',vin='',category='',confirmed_only=True,limit=25,offset=0,path=DEFAULT_DB):
    if category and category not in LABELS:raise ValueError('Categorie necunoscută.')
    registration=plate(registration);vin=str(vin or '').strip().upper();limit=max(1,min(100,int(limit)));offset=max(0,int(offset))
    where=[];params=[]
    if registration:where.append('j.registration=?');params.append(registration)
    if vin:where.append('j.vin=?');params.append(vin)
    conditions=['c.job_id=j.id']
    if category:conditions.append('c.category=?');params.append(category)
    if confirmed_only:conditions.append("c.status!='review'")
    where.append('EXISTS(SELECT 1 FROM components c WHERE '+' AND '.join(conditions)+')')
    query=' FROM jobs j WHERE '+' AND '.join(where)
    with connect(path) as db:
        total=db.execute('SELECT COUNT(*)'+query,params).fetchone()[0]
        rows=db.execute('SELECT j.payload_json'+query+' ORDER BY j.service_date DESC,CAST(j.id AS INTEGER) DESC LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
        vins=[r[0] for r in db.execute('SELECT vin FROM vehicle_vins WHERE registration=? ORDER BY vin',(registration,))] if registration else []
    jobs=[json.loads(r[0]) for r in rows]
    for j in jobs:j['components']=[c for c in j['components'] if (not category or c['category']==category) and (not confirmed_only or c['status']!='review')]
    return {'jobs':jobs,'total':total,'limit':limit,'offset':offset,'vins':vins,'ambiguous_registration':bool(registration and not vin and len(vins)>1)}


def report_csv(category='',confirmed_only=True,path=DEFAULT_DB,registration='',vin='',review_only=False):
    out=io.StringIO();writer=csv.writer(out,delimiter=';')
    writer.writerow(['Factura','Data factura','Nr inmatriculare','VIN','Vehicul','Deviz','Categorie','Confirmare','Coduri si marci piese','Cantitati si unitati','Manopera'])
    with connect(path) as db:rows=db.execute('SELECT payload_json FROM jobs ORDER BY service_date DESC,CAST(id AS INTEGER) DESC').fetchall()
    for row in rows:
        j=json.loads(row[0])
        if registration and j['registration']!=plate(registration):continue
        if vin and j['vin']!=str(vin).strip().upper():continue
        for c in j['components']:
            if category and c['category']!=category:continue
            if review_only and c['status']!='review':continue
            if not review_only and confirmed_only and c['status']=='review':continue
            for i in j['invoices']:
                writer.writerow([i['code'],i['date'],j['registration'],j['vin'],j['vehicle'],j['document_code'],c['label'],{'with_parts':'Inlocuire cu piese si manopera','labor_only':'Inlocuire: numai manopera','review':'De verificat'}[c['status']], ' | '.join(p['code']+' ['+p['manufacturer']+']' for p in c['parts']),' | '.join(f"{p['quantity']:g} {p['unit']}" for p in c['parts']),' | '.join(l['name'] for l in c['labors'])])
    return '\ufeff'+out.getvalue()
