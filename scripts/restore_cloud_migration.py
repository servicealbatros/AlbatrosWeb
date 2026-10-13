"""Validate and restore a migration into a NEW directory; never overwrite live data."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
from contextlib import closing

ROOT_FILES = {'service_requests.db', 'dsm_history.sqlite3', 'conversation_history.db',
    'accuracy_evaluations.json', 'conversation_log.jsonl', 'dsm_request_history.json',
    'last_sent_offers.json', 'manual_vehicle_hints.json', 'new_vehicle_intake_state.json',
    'offer_drafts.json', 'offer_flow_state.json', 'offer_selections.json', 'request_status.json',
    'registration_attachments.json', 'voice_reply_preferences.json', 'voice_messages.jsonl'}
FOLDERS = {'technician_output', 'tecrmi_output', 'vision_debug', 'voice_messages'}
SUFFIXES = {'.json', '.jpg', '.jpeg', '.png', '.webp', '.ogg', '.mp3', '.mp4', '.wav'}


def translate_paths(value, manifest, target, unresolved):
    if isinstance(value, dict):
        return {key: translate_paths(item, manifest, target, unresolved) for key, item in value.items()}
    if isinstance(value, list):
        return [translate_paths(item, manifest, target, unresolved) for item in value]
    if isinstance(value, str):
        normalized = value.replace('\\', '/')
        for field in ('source_data', 'source_base'):
            prefix = str(manifest.get(field, '')).replace('\\', '/').rstrip('/')
            if prefix and normalized.lower().startswith(prefix.lower() + '/'):
                relative = normalized[len(prefix) + 1:]
                if field == 'source_base' and relative.startswith('intercars_output/'):
                    relative = 'offering/' + relative.replace('intercars_output/intercars_output/', 'intercars_output/', 1)
                if relative in manifest['files']:
                    return str(target / relative)
                # This may be an old diagnostic/log path, but needs review
                # before activation; don't invent a replacement file.
                unresolved.add(relative)
                break
    return value


def allowed(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        return False
    if name in ROOT_FILES or name == 'manifest.json':
        return True
    return ((len(path.parts) == 2 and path.parts[0] in FOLDERS and path.suffix in SUFFIXES)
            or (len(path.parts) == 4 and path.parts[:3] == ('offering', 'intercars_output', 'requests')
                and path.name.startswith('request_') and path.suffix == '.json'))


def restore(source, digest, target):
    if target.exists():
        raise ValueError('Target must be a new directory; live data cannot be overwritten')
    with source.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise ValueError('Archive checksum mismatch')
    with tarfile.open(source, 'r:gz') as archive:
        members = archive.getmembers()
        names = [entry.name for entry in members]
        if len(set(names)) != len(names) or any(not entry.isfile() or not allowed(entry.name) for entry in members):
            raise ValueError('Unsupported or duplicate archive entry')
        if sum(entry.size for entry in members) > 1024 * 1024 * 1024:
            raise ValueError('Archive exceeds maximum expanded size')
        if archive.getmember('manifest.json').size > 1024 * 1024:
            raise ValueError('Manifest exceeds maximum size')
        manifest = json.load(archive.extractfile('manifest.json'))
        if manifest.get('format') != 'albatros-cloud-migration-v1' or manifest.get('contains_credentials') is not False:
            raise ValueError('Not an operational migration snapshot')
        if set(names) != set(manifest['files']) | {'manifest.json'}:
            raise ValueError('Manifest does not cover all archive files')
        # Validate every member before creating a destination.
        for entry in members:
            if entry.name != 'manifest.json':
                with archive.extractfile(entry) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['files'][entry.name]:
                        raise ValueError('Member checksum mismatch')
        target.mkdir(parents=True)
        for entry in members:
            destination = target / entry.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(entry) as stream, destination.open('wb') as output:
                while block := stream.read(1024 * 1024):
                    output.write(block)
    counts = {}
    for name in ROOT_FILES:
        if name.endswith(('.db', '.sqlite3')) and (target / name).exists():
            with closing(sqlite3.connect((target / name).as_uri() + '?mode=ro', uri=True)) as connection:
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Restored SQLite integrity failure; do not activate')
                if name == 'service_requests.db':
                    counts['requests'] = connection.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0]
                    counts['appointments'] = connection.execute('SELECT COUNT(*) FROM scheduler_appointments').fetchone()[0]
                elif name == 'dsm_history.sqlite3':
                    counts['transactions'] = connection.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
                    counts['vin_vehicles'] = connection.execute('SELECT COUNT(*) FROM vehicles').fetchone()[0]
                    counts['parts'] = connection.execute("SELECT COUNT(*) FROM lines WHERE kind='part'").fetchone()[0]
    if counts != manifest['counts']:
        raise ValueError('Counts differ from source; do not activate')
    unresolved = set()
    for relative in manifest['files']:
        path = target / relative
        if path.suffix == '.json':
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            translated = translate_paths(value, manifest, target, unresolved)
            path.write_text(json.dumps(translated, ensure_ascii=False), encoding='utf-8')
    (target / 'robot_enabled.state.json').write_text(json.dumps({'enabled': False, 'changed_by': 'cloud-migration'}))
    (target / 'migration_validation.json').write_text(json.dumps({'source_sha256': digest,
        'counts': counts, 'unresolved_paths': sorted(unresolved), 'activated': False}), encoding='utf-8')
    return {'ok': True, 'counts': counts, 'files': len(manifest['files']), 'activated': False,
            'unresolved_path_count': len(unresolved), 'requires_cloud_device_database': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--target', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(restore(args.archive.resolve(), args.sha256, args.target.resolve())))


if __name__ == '__main__':
    main()
