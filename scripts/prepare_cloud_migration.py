"""Prepare private operational data for a cloud cutover, without modifying source.

Does not upload, switch services, copy browser credentials or resume jobs.
The receiver must restore into an empty versioned directory, then verify it.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone

JSON_FILES = ('accuracy_evaluations.json', 'conversation_log.jsonl', 'dsm_request_history.json',
    'last_sent_offers.json', 'manual_vehicle_hints.json', 'new_vehicle_intake_state.json',
    'offer_drafts.json', 'offer_flow_state.json', 'offer_selections.json', 'request_status.json',
    'registration_attachments.json', 'voice_reply_preferences.json', 'voice_messages.jsonl')
DATABASES = ('service_requests.db', 'dsm_history.sqlite3', 'conversation_history.db')
ARTIFACTS = ('technician_output', 'tecrmi_output', 'vision_debug', 'voice_messages')


def snapshot(base, data, output):
    counts, entries = {}, {}
    with tempfile.TemporaryDirectory(prefix='albatros-cloud-data-') as work:
        staging = Path(work)

        def record(source, relative):
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            entries[relative] = hashlib.sha256(destination.read_bytes()).hexdigest()

        for name in DATABASES:
            source = data / name
            if not source.is_file():
                continue
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as connection:
                with closing(sqlite3.connect(staging / name)) as destination:
                    connection.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Invalid SQLite snapshot')
                if name == 'service_requests.db':
                    counts['requests'] = connection.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0]
                    counts['appointments'] = connection.execute('SELECT COUNT(*) FROM scheduler_appointments').fetchone()[0]
                if name == 'dsm_history.sqlite3':
                    counts['transactions'] = connection.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
                    counts['vin_vehicles'] = connection.execute('SELECT COUNT(*) FROM vehicles').fetchone()[0]
                    counts['parts'] = connection.execute("SELECT COUNT(*) FROM lines WHERE kind='part'").fetchone()[0]
            entries[name] = hashlib.sha256((staging / name).read_bytes()).hexdigest()
        for name in JSON_FILES:
            if (data / name).is_file():
                record(data / name, name)
        for folder in ARTIFACTS:
            for source in (data / folder).glob('*'):
                if source.is_file() and source.suffix.lower() in {'.json', '.jpg', '.jpeg', '.png',
                                                               '.webp', '.ogg', '.mp3', '.mp4', '.wav'}:
                    record(source, f'{folder}/{source.name}')
        # A previous Windows runtime nested the output folder twice. Preserve
        # the newest result per filename, with a single consistent cloud path.
        products = {}
        for folder in (base / 'intercars_output/requests', base / 'intercars_output/intercars_output/requests'):
            for source in folder.glob('request_*.json'):
                if source.is_file() and (source.name not in products or
                        source.stat().st_mtime > products[source.name].stat().st_mtime):
                    products[source.name] = source
        for name, source in products.items():
            record(source, f'offering/intercars_output/requests/{name}')
        manifest = {'format': 'albatros-cloud-migration-v1',
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'counts': counts, 'files': entries, 'contains_credentials': False,
                    'pending_jobs_copied': False, 'source_base': str(base), 'source_data': str(data)}
        (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, 'w:gz') as archive:
            for relative in (*entries, 'manifest.json'):
                archive.add(staging / relative, arcname=relative, recursive=False)
    return {'file': str(output), 'counts': counts, 'files': len(entries),
            'bytes': output.stat().st_size, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'uploaded': False, 'source_modified': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True, type=Path)
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.base.resolve(), args.data.resolve(), args.output.resolve())))


if __name__ == '__main__':
    main()
