import csv
import io
import json
from pathlib import Path
from registry.values import DataError, canonical
from bed_events.schema import parse_package


TABLES = {
    'regions': {'id': 'text', 'parent': 'nullable', 'name': 'text'},
    'facilities': {'id': 'text', 'revision': 'integer', 'recorded_at': 'text', 'name': 'text', 'region': 'text', 'open': 'text', 'close': 'text', 'ownership': 'text', 'licensed': 'boolean', 'license_limit': 'integer'},
    'events': {'id': 'text', 'revision': 'integer', 'recorded_at': 'text', 'bed': 'text', 'facility': 'text', 'effective_at': 'text', 'action': 'text', 'category': 'nullable', 'expected': 'nullable', 'cancelled': 'boolean'},
}


def decode(value, kind, path):
    if kind == 'text':
        return value
    if kind == 'nullable':
        return None if value == '' else value
    if kind == 'boolean':
        if value not in ('true', 'false'):
            raise DataError(path, 'boolean must be true or false')
        return value == 'true'
    if kind == 'integer':
        if not value or not value.isascii() or not value.isdigit():
            raise DataError(path, 'expected nonnegative decimal integer')
        return int(value)
    raise DataError(path, 'unknown column type')


def read_table(text, table):
    if table not in TABLES:
        raise ValueError('unknown import table')
    if not isinstance(text, str) or len(text.encode('utf-8')) > 50_000_000:
        raise ValueError('CSV table too large')
    schema = TABLES[table]
    try:
        reader = csv.reader(io.StringIO(text), strict=True)
        header = next(reader, None)
        if header is None or len(header) != len(set(header)) or set(header) != set(schema):
            raise DataError(table, 'CSV header must exactly match table schema')
        rows = []
        for index, cells in enumerate(reader, 2):
            if len(cells) != len(header):
                raise DataError(f'{table}:{index}', 'CSV row width mismatch')
            row = {key: decode(value, schema[key], f'{table}:{index}.{key}') for key, value in zip(header, cells)}
            rows.append(row)
            if len(rows) > 100000:
                raise DataError(table, 'too many rows')
        return rows
    except csv.Error as exc:
        raise DataError(table, 'malformed CSV') from exc


def write_table(document, table):
    parse_package(document)
    if table not in TABLES:
        raise ValueError('unknown export table')
    schema = TABLES[table]
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(schema)
    for row in document[table]:
        values = []
        for key, kind in schema.items():
            value = row[key]
            if kind == 'boolean':
                values.append('true' if value else 'false')
            elif value is None:
                values.append('')
            else:
                values.append(value)
        writer.writerow(values)
    return stream.getvalue()


def read_directory(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('expected regular directory')
    expected = {name + '.csv' for name in TABLES}
    if {path.name for path in root.iterdir()} != expected:
        raise ValueError('import directory file set mismatch')
    document = {'version': 1}
    for table in TABLES:
        path = root / (table + '.csv')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 50_000_000:
            raise ValueError('CSV input must be bounded regular file')
        document[table] = read_table(path.read_text(encoding='utf-8-sig'), table)
    parse_package(document)
    return document


def write_directory(document, directory):
    parse_package(document)
    root = Path(directory)
    if root.exists() or root.is_symlink():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    created = []
    try:
        for table in TABLES:
            path = root / (table + '.csv')
            path.write_text(write_table(document, table), encoding='utf-8')
            created.append(path)
    except BaseException:
        for path in created:
            path.unlink()
        root.rmdir()
        raise
    return {'files': sorted(path.name for path in created), 'version': 1}
