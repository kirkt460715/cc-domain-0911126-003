import hashlib
from pathlib import Path
import sqlite3
from bed_events.ledger import Ledger, IntegrityFailure


def backup(ledger, destination):
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    ledger.audit()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=False)
    output = None
    try:
        output = sqlite3.connect(str(target))
        ledger.db.backup(output)
        output.close()
        output = None
        with Ledger(target) as restored:
            summary = restored.audit()
    except BaseException:
        if output:
            output.close()
        target.unlink(missing_ok=True)
        raise
    return {'path': str(target), 'digest': hashlib.sha256(target.read_bytes()).hexdigest(), 'audit': summary}


def restore(source, destination, expected_digest):
    origin, target = Path(source), Path(destination)
    if origin.is_symlink() or not origin.is_file():
        raise ValueError('backup must be regular file')
    actual = hashlib.sha256(origin.read_bytes()).hexdigest()
    if actual != expected_digest:
        raise IntegrityFailure('backup checksum mismatch')
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    input_db = sqlite3.connect(origin.resolve().as_uri() + '?mode=ro', uri=True)
    created = False
    try:
        if input_db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise IntegrityFailure('invalid SQLite backup')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)
        created = True
        output = sqlite3.connect(str(target))
        try:
            input_db.backup(output)
        finally:
            output.close()
        with Ledger(target) as ledger:
            summary = ledger.audit()
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    finally:
        input_db.close()
    return {'path': str(target), 'audit': summary}
