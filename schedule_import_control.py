"""Persistent import timing; failures are visible and never count as success."""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import threading

_lock = threading.RLock()

def read_state(root):
    path = Path(root) / 'dsm_periodic_import.json'
    with _lock:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'interval_hours': float(os.getenv('DSM_SCHEDULE_INTERVAL_HOURS', '0')), 'enabled': True}

def write_state(root, state):
    path = Path(root) / 'dsm_periodic_import.json'
    with _lock:
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)

def import_due(state, now):
    if not state.get('enabled') or float(state.get('interval_hours') or 0) <= 0:
        return False
    due = state.get('next_due')
    return not due or now >= datetime.fromisoformat(due)

def finish_import(state, result, now):
    state.update(status='success' if result['ok'] else 'partial' if result.get('days_imported') else 'error',
                 last_finished=now.isoformat(timespec='seconds'), result=result,
                 next_due=(now + timedelta(hours=float(state.get('interval_hours') or 5)) if result['ok'] else now + timedelta(minutes=15)).isoformat(timespec='seconds'))
    if result['ok']:
        state['last_success'] = state['last_finished']
    return state
