"""Identity-only inputs for labour searches, separate from parts result files."""
import json
from pathlib import Path


def identity_path(output):
    return Path(output).with_name(Path(output).stem + '_vehicle.json')


def hints_path(output):
    return Path(output).with_name(Path(output).stem + '_hints.json')


def matching(payload, request_id, vin):
    return (isinstance(payload, dict) and payload.get('request_id') == int(request_id)
            and bool(vin) and str(payload.get('source_vin') or payload.get('vin') or '').strip().upper() == vin.strip().upper())


def read_identity(path, request_id, vin):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if matching(value, request_id, vin) else {}
