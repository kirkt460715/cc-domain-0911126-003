import base64
import json
from registry.values import canonical, fingerprint, integer, identifier
from bed_events.replay import verify_snapshot


FILTERS = {'bed', 'facility', 'category', 'region', 'ownership'}


def encode_cursor(value):
    return base64.urlsafe_b64encode(canonical(value).encode()).decode().rstrip('=')


def decode_cursor(value):
    if not isinstance(value, str) or len(value) > 20000:
        raise ValueError('invalid cursor')
    try:
        result = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('invalid cursor encoding') from exc
    if not isinstance(result, dict):
        raise ValueError('cursor must encode an object')
    return result


def beds(current, filters=None, columns=None, limit=100, cursor=None):
    verify_snapshot(current)
    filters = {} if filters is None else filters
    columns = ['bed', 'facility', 'category', 'region', 'ownership'] if columns is None else columns
    if not isinstance(filters, dict) or filters.keys() - FILTERS:
        raise ValueError('unknown bed filter')
    for key, values in filters.items():
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values) or len(values) != len(set(values)):
            raise ValueError('filter values must be unique string lists')
    if not isinstance(columns, list) or not columns or len(columns) != len(set(columns)):
        raise ValueError('projection must be nonempty unique list')
    if set(columns) - (FILTERS | {'facility_revision'}):
        raise ValueError('unknown bed projection')
    integer(limit, 'limit', 1, 1000)
    context = fingerprint({'snapshot': current['snapshot_digest'], 'filters': filters, 'columns': columns})
    rows = [row for row in current['beds'] if all(row[key] in values for key, values in filters.items())]
    rows.sort(key=lambda row: row['bed'])
    offset = 0
    if cursor is not None:
        value = decode_cursor(cursor)
        if set(value) != {'context', 'offset'} or value['context'] != context:
            raise ValueError('cursor snapshot or query mismatch')
        offset = integer(value['offset'], 'cursor.offset', 0, len(rows))
    selected = rows[offset:offset + limit]
    next_offset = offset + len(selected)
    continuation = encode_cursor({'context': context, 'offset': next_offset}) if next_offset < len(rows) else None
    return {
        'snapshot_digest': current['snapshot_digest'],
        'total': len(rows),
        'offset': offset,
        'rows': [{key: row[key] for key in columns} for row in selected],
        'next_cursor': continuation,
    }


def partition_summary(current):
    verify_snapshot(current)
    return {
        'snapshot_digest': current['snapshot_digest'],
        'active': len(current['beds']),
        'retired': len(current['retired']),
        'quarantined': len(current['quarantined']),
        'excluded': len(current['excluded']),
        'traced_beds': len(current['traces']),
        'partition_total': sum(len(current[key]) for key in ('beds', 'retired', 'quarantined', 'excluded')),
    }


def facility_drilldown(catalog, current, facility):
    identifier(facility, 'facility')
    visible = catalog.visible_facility(facility, current['known_at'])
    if visible is None:
        raise KeyError(facility)
    active = [row for row in current['beds'] if row['facility'] == facility]
    retired = [row for row in current['retired'] if row['facility'] == facility]
    excluded = [row for row in current['excluded'] if row['facility'] == facility]
    counts = {category: sum(row['category'] == category for row in active) for category in ('nursing', 'general', 'rehabilitation')}
    headroom = visible['license_limit'] - len(active)
    return {
        'facility': visible,
        'day': current['day'],
        'known_at': current['known_at'],
        'active_beds': active,
        'retired_beds': retired,
        'excluded_beds': excluded,
        'category_counts': counts,
        'license_headroom': headroom,
        'over_license': headroom < 0,
        'effective_on_day': visible['open'] <= current['day'] < visible['close'],
        'snapshot_digest': current['snapshot_digest'],
    }
