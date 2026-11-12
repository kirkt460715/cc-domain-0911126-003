import csv
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from registry.values import canonical, fingerprint
from bed_events.schema import parse_package
from bed_events.replay import snapshot, verify_snapshot
from indicators.compute import compute
from indicators.definitions import parse_definition
from indicators.disclosure import protect_regions, validate_protection
from audit.quality import quality_report, check_additivity, trace_coverage


def table_csv(rows):
    if not rows:
        return ''
    keys = sorted(set().union(*(row.keys() for row in rows)))
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, keys, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({key: canonical(value) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    return stream.getvalue()


def payload(document, current, definition):
    catalog, events = parse_package(document)
    replay = snapshot(catalog, events, current['day'], current['known_at'])
    if replay != current:
        raise ValueError('audit package snapshot does not match source replay')
    indicator = compute(catalog, current, definition)
    protected = protect_regions(catalog, indicator)
    validate_protection(catalog, protected)
    contents = {
        'source.json': canonical(document),
        'snapshot.json': canonical(current),
        'definition.json': canonical(definition.export()),
        'regional-indicator.json': canonical(indicator),
        'public-indicator.json': canonical(protected),
        'quality.json': canonical(quality_report(catalog, events, current['day'], current['known_at'])),
        'active-beds.csv': table_csv(current['beds']),
        'regional-indicator.csv': table_csv(indicator['rows']),
        'consistency.json': canonical({'additivity': check_additivity(catalog, indicator), 'trace_coverage': trace_coverage(current)}),
    }
    manifest = {
        'version': 1,
        'access': 'restricted_audit_evidence_contains_asset_identifiers',
        'snapshot_digest': current['snapshot_digest'],
        'definition_digest': definition.digest,
        'files': {name: hashlib.sha256(content.encode('utf-8')).hexdigest() for name, content in sorted(contents.items())},
    }
    contents['manifest.json'] = canonical(manifest)
    return contents


def write_package(document, current, definition, directory):
    contents = payload(document, current, definition)
    target = Path(directory)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.bed-audit-', dir=target.parent))
    try:
        for name, content in contents.items():
            with (temporary / name).open('w', encoding='utf-8', newline='') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        os.rename(temporary, target)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return verify_package(target)


def verify_package(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('audit package must be regular directory')
    manifest_file = root / 'manifest.json'
    if manifest_file.is_symlink() or not manifest_file.is_file() or manifest_file.stat().st_size > 100000:
        raise ValueError('invalid manifest')
    manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('unsupported manifest')
    names = set(manifest['files']) | {'manifest.json'}
    if {p.name for p in root.iterdir()} != names:
        raise ValueError('audit package file set mismatch')
    for name, expected in manifest['files'].items():
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe filename in manifest')
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000_000:
            raise ValueError('expected bounded regular evidence file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('audit package checksum mismatch')
    document = json.loads((root / 'source.json').read_text(encoding='utf-8'))
    current = json.loads((root / 'snapshot.json').read_text(encoding='utf-8'))
    definition = parse_definition(json.loads((root / 'definition.json').read_text(encoding='utf-8')))
    expected = payload(document, current, definition)
    if set(expected) != names:
        raise ValueError('unexpected audit package schema')
    for name, content in expected.items():
        if (root / name).read_bytes() != content.encode('utf-8'):
            raise ValueError('derived audit evidence mismatch: ' + name)
    return {'verified': True, 'snapshot_digest': current['snapshot_digest'], 'definition_digest': definition.digest, 'files': len(names)}
