import copy
from registry.values import DataError, object_fields, identifier, label, integer, boolean, iso_day, stamp, bounded_list, index_unique, fingerprint


CATEGORIES = {'nursing', 'general', 'rehabilitation'}
OWNERSHIPS = {'public', 'private', 'nonprofit'}


class Catalog:
    def __init__(self, regions, facilities):
        self.regions = copy.deepcopy(regions)
        self.facilities = copy.deepcopy(facilities)
        self.region_map = self._regions(self.regions)
        self.facility_versions = self._facilities(self.facilities)
        self.digest = fingerprint({'regions': self.regions, 'facilities': self.facilities})

    def _regions(self, rows):
        bounded_list(rows, 'regions', 10000)
        for i, row in enumerate(rows):
            path = f'regions[{i}]'
            object_fields(row, ('id', 'parent', 'name'), path=path)
            identifier(row['id'], path + '.id')
            label(row['name'], path + '.name')
            if row['parent'] is not None:
                identifier(row['parent'], path + '.parent')
        mapping = index_unique(rows, 'id', 'regions')
        for ident, row in mapping.items():
            seen = set()
            current = ident
            while current is not None:
                if current not in mapping:
                    raise DataError('regions', 'unknown parent: ' + current)
                if current in seen:
                    raise DataError('regions', 'cycle at ' + current)
                seen.add(current)
                current = mapping[current]['parent']
        return mapping

    def _facilities(self, rows):
        bounded_list(rows, 'facilities')
        groups = {}
        for i, row in enumerate(rows):
            path = f'facilities[{i}]'
            object_fields(row, ('id', 'revision', 'recorded_at', 'name', 'region', 'open', 'close', 'ownership', 'licensed', 'license_limit'), path=path)
            identifier(row['id'], path + '.id')
            integer(row['revision'], path + '.revision', 1)
            stamp(row['recorded_at'], path + '.recorded_at')
            label(row['name'], path + '.name')
            if row['region'] not in self.region_map:
                raise DataError(path + '.region', 'unknown region')
            iso_day(row['open'], path + '.open')
            iso_day(row['close'], path + '.close')
            if row['open'] >= row['close']:
                raise DataError(path, 'facility validity must be positive')
            if row['ownership'] not in OWNERSHIPS:
                raise DataError(path, 'unknown ownership')
            boolean(row['licensed'], path + '.licensed')
            integer(row['license_limit'], path + '.license_limit')
            groups.setdefault(row['id'], []).append(row)
        for ident, versions in groups.items():
            versions.sort(key=lambda row: row['revision'])
            if [row['revision'] for row in versions] != list(range(1, len(versions) + 1)):
                raise DataError('facilities.' + ident, 'revisions must be contiguous starting at 1')
            if any(a['recorded_at'] >= b['recorded_at'] for a, b in zip(versions, versions[1:])):
                raise DataError('facilities.' + ident, 'revision knowledge times must strictly increase')
        return groups

    def visible_facility(self, ident, known_at):
        stamp(known_at, 'known_at')
        if ident not in self.facility_versions:
            raise KeyError(ident)
        versions = [r for r in self.facility_versions[ident] if r['recorded_at'] <= known_at]
        return copy.deepcopy(versions[-1]) if versions else None

    def at(self, day, known_at):
        iso_day(day, 'day')
        stamp(known_at, 'known_at')
        result = {}
        for ident in sorted(self.facility_versions):
            visible = self.visible_facility(ident, known_at)
            if visible and visible['open'] <= day < visible['close']:
                result[ident] = visible
        return result

    def ancestors(self, region):
        if region not in self.region_map:
            raise KeyError(region)
        result = []
        while region is not None:
            result.append(region)
            region = self.region_map[region]['parent']
        return result

    def descendants(self, region):
        if region not in self.region_map:
            raise KeyError(region)
        return sorted(ident for ident in self.region_map if region in self.ancestors(ident))

    def export(self):
        return {'regions': copy.deepcopy(self.regions), 'facilities': copy.deepcopy(self.facilities)}

    def revisions_between(self, start, end):
        stamp(start, 'start')
        stamp(end, 'end')
        if start > end:
            raise DataError('range', 'knowledge range reversed')
        rows = [copy.deepcopy(row) for versions in self.facility_versions.values() for row in versions if start < row['recorded_at'] <= end]
        return sorted(rows, key=lambda row: (row['recorded_at'], row['id'], row['revision']))
