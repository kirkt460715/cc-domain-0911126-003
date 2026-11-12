from collections import defaultdict
from registry.values import next_day, day_range, decimal_ratio
from bed_events.replay import snapshot
from indicators.compute import compute


def stock_flow_bridge(catalog, events, first_day, last_day, known_at, definition):
    if first_day >= last_day:
        raise ValueError('stock bridge requires increasing business days')
    before = snapshot(catalog, events, first_day, known_at)
    after = snapshot(catalog, events, last_day, known_at)
    a = {row['bed']: row for row in before['beds']}
    b = {row['bed']: row for row in after['beds']}
    movements = []
    by_region = defaultdict(lambda: {'numerator_entries': 0, 'numerator_exits': 0, 'denominator_entries': 0, 'denominator_exits': 0})
    def memberships(row):
        if row is None or row['ownership'] not in definition.ownerships or row['category'] not in definition.denominator:
            return {}
        return {region: (int(row['category'] in definition.numerator), 1) for region in catalog.ancestors(row['region'])}
    for bed in sorted(a.keys() | b.keys()):
        old = memberships(a.get(bed))
        new = memberships(b.get(bed))
        if old == new:
            continue
        for region in sorted(old.keys() | new.keys()):
            old_n, old_d = old.get(region, (0, 0))
            new_n, new_d = new.get(region, (0, 0))
            n_delta, d_delta = new_n - old_n, new_d - old_d
            if not n_delta and not d_delta:
                continue
            group = by_region[region]
            group['numerator_entries'] += max(0, n_delta)
            group['numerator_exits'] += max(0, -n_delta)
            group['denominator_entries'] += max(0, d_delta)
            group['denominator_exits'] += max(0, -d_delta)
            movements.append({'bed': bed, 'region': region, 'numerator_change': n_delta, 'denominator_change': d_delta})
    old_indicator = {row['region']: row for row in compute(catalog, before, definition)['rows']}
    new_indicator = {row['region']: row for row in compute(catalog, after, definition)['rows']}
    rows = []
    for region in sorted(catalog.region_map):
        group = by_region[region]
        old = old_indicator[region]
        new = new_indicator[region]
        for metric in ('numerator', 'denominator'):
            expected = old[metric] + group[metric + '_entries'] - group[metric + '_exits']
            if expected != new[metric]:
                raise AssertionError('stock-flow bridge failed for ' + region)
        rows.append({'region': region, 'opening_numerator': old['numerator'], 'opening_denominator': old['denominator'], **group, 'closing_numerator': new['numerator'], 'closing_denominator': new['denominator']})
    return {
        'first_day': first_day,
        'last_day': last_day,
        'known_at': known_at,
        'definition_digest': definition.digest,
        'before_digest': before['snapshot_digest'],
        'after_digest': after['snapshot_digest'],
        'rows': rows,
        'movements': movements,
        'quality_resolved': before['publishable'] and after['publishable'],
        'interpretation': 'net_endpoint_membership_changes_not_gross_event_counts',
    }


def daily_capacity_utilization(catalog, events, start, end, known_at):
    rows = []
    for day in day_range(start, end):
        current = snapshot(catalog, events, day, known_at)
        facilities = catalog.at(day, known_at)
        active = defaultdict(int)
        nursing = defaultdict(int)
        for bed in current['beds']:
            active[bed['facility']] += 1
            nursing[bed['facility']] += bed['category'] == 'nursing'
        for ident, facility in sorted(facilities.items()):
            if not facility['licensed']:
                continue
            limit = facility['license_limit']
            rows.append({
                'day': day,
                'facility': ident,
                'region': facility['region'],
                'license_limit': limit,
                'active': active[ident],
                'nursing': nursing[ident],
                'license_utilization': decimal_ratio(active[ident], limit),
                'remaining_license_capacity': max(0, limit - active[ident]),
                'over_license_beds': max(0, active[ident] - limit),
                'quality_resolved': current['publishable'],
            })
    return {'known_at': known_at, 'start': start, 'end': end, 'rows': rows, 'interpretation': 'registered_bed_supply_not_patient_occupancy'}
