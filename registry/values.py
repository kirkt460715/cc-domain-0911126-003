from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re


class DataError(ValueError):
    def __init__(self, path, message):
        self.path = path
        self.message = message
        super().__init__(f'{path}: {message}')


def object_fields(value, required, optional=(), path='$'):
    if not isinstance(value, dict):
        raise DataError(path, 'expected object')
    absent = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if absent:
        raise DataError(path, 'missing fields: ' + ','.join(sorted(absent)))
    if extra:
        raise DataError(path, 'unknown fields: ' + ','.join(sorted(extra)))
    return value


def identifier(value, path):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}', value):
        raise DataError(path, 'expected bounded ASCII identifier')
    return value


def label(value, path):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise DataError(path, 'invalid label')
    return value


def integer(value, path, low=0, high=10**9):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DataError(path, f'expected integer in {low}..{high}')
    return value


def boolean(value, path):
    if not isinstance(value, bool):
        raise DataError(path, 'expected boolean')
    return value


def iso_day(value, path):
    if not isinstance(value, str):
        raise DataError(path, 'expected ISO day')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DataError(path, 'invalid calendar day') from exc
    if parsed.isoformat() != value:
        raise DataError(path, 'expected YYYY-MM-DD')
    return value


def stamp(value, path):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value):
        raise DataError(path, 'expected UTC second precision timestamp')
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError as exc:
        raise DataError(path, 'invalid UTC timestamp') from exc
    return value


def next_day(value):
    return (date.fromisoformat(iso_day(value, 'day')) + timedelta(days=1)).isoformat()


def day_range(start, end, maximum=3660):
    first = date.fromisoformat(iso_day(start, 'start'))
    last = date.fromisoformat(iso_day(end, 'end'))
    count = (last - first).days
    if count < 1 or count > maximum:
        raise DataError('range', f'expected 1..{maximum} days in half-open interval')
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def decimal_ratio(numerator, denominator):
    if denominator == 0:
        return None
    return format(Decimal(numerator) / Decimal(denominator), '.12f')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def bounded_list(value, path, maximum=100000):
    if not isinstance(value, list) or len(value) > maximum:
        raise DataError(path, f'expected list with at most {maximum} rows')
    return value


def index_unique(rows, key, path):
    result = {}
    for row in rows:
        value = row[key]
        if value in result:
            raise DataError(path, 'duplicate key: ' + str(value))
        result[value] = row
    return result
