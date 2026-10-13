"""Private recovery snapshots; never used as a second writable live database."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
from datetime import datetime, timezone

from starlette.responses import JSONResponse


async def receive_backup(request):
    folder = Path(os.environ['ALBATROS_DATA_DIR']) / 'worker_backups'
    folder.mkdir(parents=True, exist_ok=True)
    size, digest = 0, hashlib.sha256()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, suffix='.upload', delete=False) as output:
            temporary = Path(output.name)
            async for chunk in request.stream():
                size += len(chunk)
                if size > 256 * 1024 * 1024:
                    raise ValueError('Backup too large')
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != request.headers.get('x-backup-sha256'):
            raise ValueError('Checksum mismatch')
        with tarfile.open(temporary, 'r:gz') as archive:
            total = 0
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if not member.isfile() or path.is_absolute() or '..' in path.parts or path.name.startswith('.env'):
                    raise ValueError('Invalid archive entry')
                total += member.size
                if total > 1024 * 1024 * 1024:
                    raise ValueError('Uncompressed backup too large')
            member = archive.getmember('manifest.json')
            if member.size > 65536:
                raise ValueError('Manifest too large')
            manifest = json.load(archive.extractfile(member))
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + digest.hexdigest()[:12] + '.tar.gz'
        destination = folder / name
        temporary.replace(destination)
        info = {'ok': True, 'sha256': digest.hexdigest(), 'bytes': size,
                'saved_at': datetime.now(timezone.utc).isoformat(),
                'counts': manifest.get('counts', {}), 'file': name}
        (folder / 'latest.json').write_text(json.dumps(info), encoding='utf-8')
        return JSONResponse(info)
    except (ValueError, KeyError, tarfile.TarError, json.JSONDecodeError):
        return JSONResponse({'detail': 'Backup invalid; copia existentă a fost păstrată.'}, status_code=400)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
