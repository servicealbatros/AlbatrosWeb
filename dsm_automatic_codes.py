"""Read historical part packages for one vehicle; usable by UI and local automation."""
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import dsm_brake_packages as brakes
import dsm_revision_packages as revisions

WORKS = {'revision': 'Revizie', **brakes.LABELS}
VIN = re.compile(r'[A-HJ-NPR-Z0-9]{17}')


def catalogue_paths():
    configured = os.getenv('ALBATROS_PACKAGE_DATA_DIR')
    if configured:
        root = Path(configured)
    elif os.getenv('ALBATROS_DATA_DIR'):
        root = Path(os.environ['ALBATROS_DATA_DIR']) / 'packages'
    else:
        root = Path(__file__).resolve().parent / 'data'
    return root/'dsm_revision_packages.sqlite3', root/'dsm_brake_packages.sqlite3'


def vehicles(query='', limit=20, revision_db=None, brake_db=None):
    """Find individual VINs for an advisor to select; never merge reused plates."""
    q = revisions.plate(query)
    limit = max(1, min(30, int(limit)))
    if len(q) < 2:
        return {'vehicles': [], 'has_more': False}
    paths = catalogue_paths()
    found = {}
    truncated = False
    for path, table in ((revision_db or paths[0], 'packages'), (brake_db or paths[1], 'jobs')):
        if not Path(path).is_file():
            raise FileNotFoundError('Baza DSM nu este disponibilă.')
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
            vins = [r[0] for r in db.execute(f'SELECT DISTINCT vin FROM {table} WHERE (registration LIKE ? OR vin LIKE ?) AND length(vin)=17 ORDER BY vin LIMIT ?', ('%'+q+'%', '%'+q+'%', limit+1)) if VIN.fullmatch(r[0] or '')]
            truncated |= len(vins) > limit
            if not vins:
                continue
            rows = db.execute(f'SELECT payload_json FROM {table} WHERE vin IN ({",".join("?" for _ in vins)})', vins).fetchall()
        for row in rows:
            p = json.loads(row[0])
            if not p.get('invoices'):
                continue
            v = found.setdefault(p['vin'], {'vin': p['vin'], 'registrations': set(), 'vehicle': '', 'registration': '', 'service_date': '', 'works': set()})
            v['registrations'].add(p['registration'])
            if (p.get('service_date') or '') >= v['service_date']:
                v.update(vehicle=p.get('vehicle') or 'Vehicul', registration=p['registration'], service_date=p.get('service_date') or '')
            if table == 'packages':
                v['works'].add('revision')
            else:
                v['works'].update(c['category'] for c in p['components'] if c['status'] != 'review')
    values = sorted(found.values(), key=lambda v: (v['registration'], v['vin']))
    return {'vehicles': [{**v, 'registrations': sorted(v['registrations']), 'works': [w for w in WORKS if w in v['works']]} for v in values[:limit]], 'has_more': truncated or len(values)>limit}


def _read(path, table, registration, vin):
    if not Path(path).is_file():
        raise FileNotFoundError('Baza DSM nu este disponibilă.')
    # Lookup must never create or modify a catalogue.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        known = {r[0] for r in db.execute('SELECT vin FROM vehicle_vins WHERE registration=?', (registration,))} if registration else set()
        field, value = ('vin', vin) if vin else ('registration', registration)
        rows = db.execute(f'SELECT payload_json FROM {table} WHERE {field}=? ORDER BY service_date DESC,CAST(id AS INTEGER) DESC', (value,)).fetchall()
    return known, [json.loads(r[0]) for r in rows]


def lookup(registration='', vin='', work='all', revision_db=None, brake_db=None):
    paths = catalogue_paths()
    revision_db, brake_db = revision_db or paths[0], brake_db or paths[1]
    registration = revisions.plate(registration)
    vin = str(vin or '').strip().upper()
    if work != 'all' and work not in WORKS:
        raise ValueError('Lucrare necunoscută.')
    if not registration and not vin:
        raise ValueError('Completează numărul de înmatriculare sau VIN-ul.')
    if vin and not VIN.fullmatch(vin):
        raise ValueError('VIN-ul trebuie să conțină 17 caractere valide.')
    rk, rp = _read(revision_db, 'packages', registration, vin)
    bk, bp = _read(brake_db, 'jobs', registration, vin)
    known = sorted(v for v in rk | bk if VIN.fullmatch(v or ''))
    result = {'registration': registration, 'vin': vin, 'vins': known, 'work': work, 'packages': [],
              'selection_rule': 'Cea mai recentă lucrare facturată din fiecare categorie; fără combinarea devizelor.'}
    if registration and vin and known and vin not in known:
        return {**result, 'status': 'identity_mismatch', 'message': 'Numărul și VIN-ul nu corespund istoricului. Verifică identificarea mașinii.'}
    if not vin:
        if len(known) != 1:
            return {**result, 'status': 'needs_vin', 'message': 'Introdu VIN-ul din talon pentru identificarea sigură a mașinii.'}
        vin = known[0]
        result['vin'] = vin
        # Also find history under previous registrations of this same vehicle.
        _, rp = _read(revision_db, 'packages', '', vin)
        _, bp = _read(brake_db, 'jobs', '', vin)
    for category in WORKS if work == 'all' else [work]:
        candidates = []
        if category == 'revision':
            candidates = [(p, p) for p in rp if p.get('invoices')]
        else:
            for job in bp:
                component = next((c for c in job['components'] if c['category'] == category and c['status'] != 'review'), None)
                if component and job.get('invoices'):
                    candidates.append((job, component))
        if not candidates:
            result['packages'].append({'work': category, 'label': WORKS[category], 'status': 'not_found', 'parts': [], 'issues': ['Nu există o lucrare facturată confirmată în această categorie.']})
            continue
        source, component = candidates[0]
        if category == 'revision':
            component = revisions.validate_package(component)
        ready = bool(component.get('ready') and component.get('parts'))
        result['packages'].append({
            'work': category, 'label': WORKS[category], 'status': 'ready' if ready else 'needs_review',
            'source_id': str(source['transaction_id']), 'document_code': source['document_code'],
            'service_date': source.get('service_date'), 'registration': source['registration'], 'vin': source['vin'],
            'vehicle': source.get('vehicle'), 'invoices': source['invoices'],
            'labors': component.get('labors', []), 'issues': component.get('issues', []),
            'parts': component['parts'] if ready else [],
        })
    ready_count = sum(p['status'] == 'ready' for p in result['packages'])
    status = 'ready' if ready_count == len(result['packages']) else 'partial' if ready_count else 'unavailable'
    return {**result, 'status': status, 'message': 'Coduri istorice, cu marca și unitatea din DSM. Verifică VIN-ul și lucrarea înainte de reutilizare.'}


def package_text(package):
    if package['status'] != 'ready':
        raise ValueError('Pachetul nu este pregătit pentru preluare automată.')
    return 'Cod\tMarca\tCantitate\tUnitate\n' + revisions.export_text(package)
