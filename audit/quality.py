from collections import defaultdict
from registry.values import fingerprint, iso_day, stamp, integer
from bed_events.replay import snapshot, verify_snapshot
from indicators.compute import compute


def quality_report(catalog, events, day, known_at):
    current = snapshot(catalog, events, day, known_at)
    findings = []
    for bed in current['quarantined']:
        for conflict in bed['conflicts']:
            findings.append({'severity': 'error', 'entity': bed['bed'], 'code': conflict['code'], 'events': conflict['events']})
    for item in current['license_violations']:
        findings.append({'severity': 'error', 'entity': item['facility'], 'code': 'license_capacity_exceeded', 'details': item})
    for item in current['excluded']:
        findings.append({'severity': 'warning', 'entity': item['bed'], 'code': item['reason']})
    visible = events.visible(known_at, True)
    cancellations = [row for row in visible if row['cancelled']]
    for row in cancellations:
        findings.append({'severity': 'info', 'entity': row['bed'], 'code': 'logical_event_cancelled', 'event': row['id'], 'revision': row['revision']})
    active_facilities = catalog.at(day, known_at)
    present = {row['facility'] for row in current['beds']}
    for ident, facility in active_facilities.items():
        if facility['licensed'] and ident not in present:
            findings.append({'severity': 'warning', 'entity': ident, 'code': 'licensed_facility_without_countable_beds'})
    late_events = []
    for row in visible:
        if row['recorded_at'][:10] > row['effective_at'][:10]:
            delay = (__import__('datetime').date.fromisoformat(row['recorded_at'][:10]) - __import__('datetime').date.fromisoformat(row['effective_at'][:10])).days
            late_events.append({'event': row['id'], 'revision': row['revision'], 'late_days': delay})
    return {
        'snapshot_digest': current['snapshot_digest'],
        'publishable': current['publishable'],
        'counts': {level: sum(row['severity'] == level for row in findings) for level in ('error', 'warning', 'info')},
        'findings': sorted(findings, key=lambda row: (row['severity'], row['entity'], row['code'])),
        'late_events': late_events,
        'maximum_lateness_days': max((r['late_days'] for r in late_events), default=0),
        'visible_logical_events': len(visible),
        'cancelled_logical_events': len(cancellations),
    }


def check_additivity(catalog, indicator):
    if indicator['level'] != 'region':
        raise ValueError('additivity check requires regional indicator')
    rows = {row['region']: row for row in indicator['rows']}
    children = defaultdict(list)
    for ident, region in catalog.region_map.items():
        if region['parent']:
            children[region['parent']].append(ident)
    errors = []
    for parent, child_ids in sorted(children.items()):
        for key in ('numerator', 'denominator'):
            child_total = sum(rows[ident][key] for ident in child_ids)
            if child_total > rows[parent][key]:
                errors.append({'region': parent, 'metric': key, 'code': 'children_exceed_parent'})
    return {'valid': not errors, 'findings': errors, 'allows_direct_parent_attribution': True}


def trace_coverage(current):
    verify_snapshot(current)
    trace_beds = {row['bed'] for row in current['traces']}
    partition_beds = set()
    for partition in ('beds', 'quarantined', 'retired', 'excluded'):
        partition_beds.update(row['bed'] for row in current[partition])
    missing = sorted(partition_beds - trace_beds)
    return {'valid': not missing, 'missing_traces': missing, 'traced_beds': len(trace_beds), 'partitioned_beds': len(partition_beds)}


def revision_inventory(catalog, events):
    event_rows = []
    for ident, versions in sorted(events.versions.items()):
        latest = versions[-1]
        event_rows.append({
            'event': ident,
            'bed': latest['bed'],
            'facility': latest['facility'],
            'revisions': len(versions),
            'first_known': versions[0]['recorded_at'],
            'last_known': latest['recorded_at'],
            'latest_cancelled': latest['cancelled'],
            'effective_time_changed': len({row['effective_at'] for row in versions}) > 1,
            'action_changed': len({row['action'] for row in versions}) > 1,
        })
    facility_rows = []
    for ident, versions in sorted(catalog.facility_versions.items()):
        facility_rows.append({
            'facility': ident,
            'revisions': len(versions),
            'region_changed': len({row['region'] for row in versions}) > 1,
            'ownership_changed': len({row['ownership'] for row in versions}) > 1,
            'license_changed': len({(row['licensed'], row['license_limit']) for row in versions}) > 1,
        })
    return {'event_revisions': event_rows, 'facility_revisions': facility_rows}
