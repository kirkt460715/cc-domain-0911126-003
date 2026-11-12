from collections import defaultdict
from registry.values import decimal_ratio, fingerprint
from bed_events.replay import verify_snapshot


def measures(numerator, denominator, definition):
    if numerator < 0 or denominator < numerator:
        raise ValueError('invalid indicator counts')
    target = definition.target_basis_points
    required = (denominator * target + 9999) // 10000
    converted_gap = max(0, required - numerator)
    if target == 10000:
        addition_gap = 0 if numerator == denominator else None
    elif numerator * 10000 >= denominator * target:
        addition_gap = 0
    else:
        shortfall = denominator * target - numerator * 10000
        addition_gap = (shortfall + 10000 - target - 1) // (10000 - target)
    return {
        'numerator': numerator,
        'denominator': denominator,
        'ratio': decimal_ratio(numerator, denominator),
        'ratio_basis_points_floor': numerator * 10000 // denominator if denominator else None,
        'target_basis_points': target,
        'meets_target': numerator * 10000 >= denominator * target if denominator else None,
        'small_denominator': 0 < denominator < definition.small_denominator,
        'conversion_gap': converted_gap if denominator else None,
        'additional_nursing_gap': addition_gap if denominator else None,
    }


def compute(catalog, snapshot, definition, level='region'):
    verify_snapshot(snapshot)
    if level not in ('region', 'facility', 'ownership'):
        raise ValueError('unknown indicator level')
    groups = {}
    if level == 'region':
        groups = {ident: [0, 0, set()] for ident in catalog.region_map}
    elif level == 'ownership':
        groups = {ident: [0, 0, set()] for ident in definition.ownerships}
    else:
        facilities = catalog.at(snapshot['day'], snapshot['known_at'])
        groups = {ident: [0, 0, set()] for ident, row in facilities.items() if row['licensed'] and row['ownership'] in definition.ownerships}
    included = []
    excluded_policy = []
    for row in snapshot['beds']:
        if row['ownership'] not in definition.ownerships or row['category'] not in definition.denominator:
            excluded_policy.append(row['bed'])
            continue
        if level == 'region':
            keys = catalog.ancestors(row['region'])
        elif level == 'ownership':
            keys = [row['ownership']]
        else:
            keys = [row['facility']]
        included.append(row['bed'])
        for key in keys:
            group = groups.setdefault(key, [0, 0, set()])
            group[1] += 1
            group[0] += row['category'] in definition.numerator
            group[2].add(row['facility'])
    results = []
    for key, (numerator, denominator, facilities) in sorted(groups.items()):
        results.append({
            level: key,
            **measures(numerator, denominator, definition),
            'facility_count_with_beds': len(facilities),
        })
    return {
        'day': snapshot['day'],
        'known_at': snapshot['known_at'],
        'snapshot_digest': snapshot['snapshot_digest'],
        'definition': definition.export(),
        'definition_digest': definition.digest,
        'level': level,
        'rows': results,
        'included_beds': sorted(included),
        'excluded_by_policy': sorted(excluded_policy),
        'quality': {
            'quarantined_beds': len(snapshot['quarantined']),
            'excluded_beds': len(snapshot['excluded']),
            'license_violations': len(snapshot['license_violations']),
            'publishable': snapshot['publishable'],
        },
    }


def compare_indicators(before, after):
    if before['definition_digest'] != after['definition_digest'] or before['level'] != after['level']:
        raise ValueError('cannot compare different indicator definitions or levels')
    level = before['level']
    old = {row[level]: row for row in before['rows']}
    new = {row[level]: row for row in after['rows']}
    result = []
    for key in sorted(old.keys() | new.keys()):
        a, b = old.get(key), new.get(key)
        if a is None or b is None:
            result.append({level: key, 'state': 'unpaired'})
            continue
        result.append({
            level: key,
            'state': 'paired',
            'numerator_change': b['numerator'] - a['numerator'],
            'denominator_change': b['denominator'] - a['denominator'],
            'before_ratio': a['ratio'],
            'after_ratio': b['ratio'],
            'before_meets_target': a['meets_target'],
            'after_meets_target': b['meets_target'],
        })
    return {'definition_digest': before['definition_digest'], 'level': level, 'rows': result}
