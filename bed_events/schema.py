import copy
from registry.catalog import Catalog, CATEGORIES
from registry.values import object_fields, identifier, integer, boolean, iso_day, stamp, bounded_list, DataError, fingerprint


ACTIONS = {'commission', 'reclassify', 'retire', 'reopen'}


def validate_event(row, path='event'):
    object_fields(row, ('id', 'revision', 'recorded_at', 'bed', 'facility', 'effective_at', 'action', 'category', 'expected', 'cancelled'), path=path)
    for key in ('id', 'bed', 'facility'):
        identifier(row[key], path + '.' + key)
    integer(row['revision'], path + '.revision', 1)
    stamp(row['recorded_at'], path + '.recorded_at')
    stamp(row['effective_at'], path + '.effective_at')
    if row['action'] not in ACTIONS:
        raise DataError(path, 'unknown action')
    if row['category'] is not None and row['category'] not in CATEGORIES:
        raise DataError(path, 'unknown bed category')
    if row['expected'] is not None and row['expected'] not in CATEGORIES | {'retired'}:
        raise DataError(path, 'unknown expected prior category')
    boolean(row['cancelled'], path + '.cancelled')
    if row['action'] in ('commission', 'reopen', 'reclassify') and row['category'] is None:
        raise DataError(path, 'active action requires category')
    if row['action'] == 'commission' and row['expected'] is not None:
        raise DataError(path, 'commission expects no prior bed')
    if row['action'] == 'reopen' and row['expected'] != 'retired':
        raise DataError(path, 'reopen must expect retired')
    if row['action'] == 'retire' and row['category'] is not None:
        raise DataError(path, 'retire category must be null')
    if row['action'] in ('retire', 'reclassify') and row['expected'] not in CATEGORIES:
        raise DataError(path, 'action must name prior active category')
    return copy.deepcopy(row)


class EventSet:
    def __init__(self, rows, catalog):
        bounded_list(rows, 'events')
        self.rows = [validate_event(row, f'events[{i}]') for i, row in enumerate(rows)]
        self.versions = {}
        for row in self.rows:
            if row['facility'] not in catalog.facility_versions:
                raise DataError('events', 'unknown facility ' + row['facility'])
            self.versions.setdefault(row['id'], []).append(row)
        for ident, versions in self.versions.items():
            versions.sort(key=lambda r: r['revision'])
            if [row['revision'] for row in versions] != list(range(1, len(versions) + 1)):
                raise DataError('events.' + ident, 'event revisions must be contiguous starting at 1')
            if any(a['recorded_at'] >= b['recorded_at'] for a, b in zip(versions, versions[1:])):
                raise DataError('events.' + ident, 'knowledge time must strictly increase')
            origin = versions[0]
            if any(row['bed'] != origin['bed'] or row['facility'] != origin['facility'] for row in versions):
                raise DataError('events.' + ident, 'revision cannot change bed identity or facility')
        self.digest = fingerprint(sorted(self.rows, key=lambda row: (row['id'], row['revision'])))

    def visible(self, known_at, include_cancelled=False):
        stamp(known_at, 'known_at')
        result = []
        for ident, versions in sorted(self.versions.items()):
            visible = [row for row in versions if row['recorded_at'] <= known_at]
            if not visible:
                continue
            row = visible[-1]
            if row['cancelled'] and not include_cancelled:
                continue
            result.append(copy.deepcopy(row))
        return sorted(result, key=lambda row: (row['effective_at'], row['id']))

    def changed_between(self, first, second):
        stamp(first, 'first')
        stamp(second, 'second')
        if first > second:
            raise DataError('range', 'reversed knowledge range')
        return sorted([copy.deepcopy(row) for row in self.rows if first < row['recorded_at'] <= second], key=lambda row: (row['recorded_at'], row['id'], row['revision']))


def parse_package(document):
    object_fields(document, ('version', 'regions', 'facilities', 'events'), path='$')
    if type(document['version']) is not int or document['version'] != 1:
        raise DataError('version', 'only version 1 supported')
    catalog = Catalog(document['regions'], document['facilities'])
    events = EventSet(document['events'], catalog)
    return catalog, events
