"""Send a consistent SQLite recovery snapshot to the private Render disk.

Reads only known application files. Supplier profiles, credentials, device
sessions and runnable programs are excluded. The cloud token comes from the
calling process environment and is never included in the archive.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone

import httpx

JSON_FILES = ('accuracy_evaluations.json', 'conversation_log.jsonl', 'dsm_request_history.json',
    'last_sent_offers.json', 'manual_vehicle_hints.json', 'new_vehicle_intake_state.json',
    'offer_drafts.json', 'offer_flow_state.json', 'offer_selections.json', 'request_status.json',
    'registration_attachments.json', 'voice_reply_preferences.json')
DATABASES = ('service_requests.db', 'dsm_history.sqlite3', 'conversation_history.db', 'scheduler.db')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True, type=Path)
    args = parser.parse_args()
    token = os.environ['ALBATROS_CLOUD_ADMIN']
    counts = {}
    with tempfile.TemporaryDirectory(prefix='albatros-recovery-') as work:
        staging = Path(work)
        for name in DATABASES:
            source = args.data / name
            if not source.is_file():
                continue
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
                with closing(sqlite3.connect(staging / name)) as destination:
                    db.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Source database integrity check failed')
                if name == 'service_requests.db':
                    counts['requests'] = db.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0]
                if name == 'dsm_history.sqlite3':
                    counts.update(transactions=db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],
                                  vin_vehicles=db.execute('SELECT COUNT(*) FROM vehicles').fetchone()[0],
                                  parts=db.execute("SELECT COUNT(*) FROM lines WHERE kind='part'").fetchone()[0])
        for name in JSON_FILES:
            source = args.data / name
            if source.is_file():
                (staging / name).write_bytes(source.read_bytes())
        (staging / 'manifest.json').write_text(json.dumps({'created_at': datetime.now(timezone.utc).isoformat(),
            'counts': counts, 'operational_source': 'owned-windows-worker', 'contains_credentials': False}), encoding='utf-8')
        archive = staging / 'snapshot.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for path in staging.iterdir():
                if path != archive:
                    tar.add(path, arcname=path.name, recursive=False)
        with archive.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        def chunks():
            with archive.open('rb') as stream:
                while data := stream.read(1024 * 1024):
                    yield data
        with httpx.Client(timeout=httpx.Timeout(180, connect=15)) as client:
            result = client.post('https://albatros-backend-vbc0.onrender.com/api/admin/worker-backup',
                headers={'Authorization': 'Bearer ' + token, 'X-Backup-SHA256': digest,
                         'Content-Type': 'application/gzip', 'Content-Length': str(archive.stat().st_size)}, content=chunks())
            result.raise_for_status()
            info = result.json()
            if info.get('sha256') != digest:
                raise RuntimeError('Cloud backup checksum did not match')
            print(json.dumps(info))


if __name__ == '__main__':
    main()
