from collections import defaultdict
import copy
from registry.values import iso_day, stamp, next_day, fingerprint
from registry.catalog import CATEGORIES


def apply_event(state, event):
    action = event['action']
    if action == 'commission':
        if state is not None:
            return state, 'commission_existing_bed'
        return {'facility': event['facility'], 'category': event['category'], 'active': True}, None
    if state is None:
        return state, 'action_before_commission'
    if state['facility'] != event['facility']:
        return state, 'facility_changed_without_transfer_protocol'
    current = state['category'] if state['active'] else 'retired'
    if current != event['expected']:
        return state, 'prior_category_mismatch'
    if action == 'reclassify':
        return {**state, 'category': event['category'], 'active': True}, None
    if action == 'retire':
        return {**state, 'active': False}, None
    if action == 'reopen':
        return {**state, 'category': event['category'], 'active': True}, None
    raise ValueError('unvalidated bed action')


def replay_bed(events, cutoff):
    selected = [row for row in events if row['effective_at'] < cutoff]
    selected.sort(key=lambda row: (row['effective_at'], row['id']))
    at_time = defaultdict(list)
    for row in selected:
        at_time[row['effective_at']].append(row)
    state = None
    trace = []
    conflicts = []
    for effective_at, group in sorted(at_time.items()):
        if len(group) > 1:
            conflicts.append({'code': 'simultaneous_bed_events', 'effective_at': effective_at, 'events': sorted(row['id'] for row in group)})
            for event in group:
                trace.append({'event': event['id'], 'revision': event['revision'], 'state': 'conflict', 'code': 'simultaneous_bed_events'})
            continue
        event = group[0]
        previous = copy.deepcopy(state)
        candidate, error = apply_event(state, event)
        if error:
            conflicts.append({'code': error, 'effective_at': effective_at, 'events': [event['id']]})
            trace.append({'event': event['id'], 'revision': event['revision'], 'state': 'conflict', 'code': error, 'before': previous})
        else:
            state = candidate
            trace.append({'event': event['id'], 'revision': event['revision'], 'state': 'applied', 'before': previous, 'after': copy.deepcopy(state)})
    return {'state': state, 'trace': trace, 'conflicts': conflicts, 'quarantined': bool(conflicts)}


def snapshot(catalog, events, day, known_at):
    iso_day(day, 'day')
    stamp(known_at, 'known_at')
    cutoff = next_day(day) + 'T00:00:00Z'
    visible = events.visible(known_at)
    by_bed = defaultdict(list)
    for row in visible:
        if row['effective_at'] < cutoff:
            by_bed[row['bed']].append(row)
    facilities = catalog.at(day, known_at)
    beds = []
    quarantined = []
    retired = []
    excluded = []
    traces = []
    for bed, history in sorted(by_bed.items()):
        replay = replay_bed(history, cutoff)
        traces.append({'bed': bed, 'trace': replay['trace']})
        if replay['quarantined']:
            quarantined.append({'bed': bed, 'conflicts': replay['conflicts']})
            continue
        state = replay['state']
        if state is None:
            continue
        if not state['active']:
            retired.append({'bed': bed, **state})
            continue
        facility = facilities.get(state['facility'])
        if facility is None:
            excluded.append({'bed': bed, 'facility': state['facility'], 'reason': 'facility_not_effective_or_not_known'})
            continue
        if not facility['licensed']:
            excluded.append({'bed': bed, 'facility': state['facility'], 'reason': 'facility_unlicensed'})
            continue
        beds.append({
            'bed': bed,
            'facility': state['facility'],
            'category': state['category'],
            'region': facility['region'],
            'ownership': facility['ownership'],
            'facility_revision': facility['revision'],
        })
    counts = defaultdict(int)
    for row in beds:
        counts[row['facility']] += 1
    license_violations = []
    for ident, count in sorted(counts.items()):
        if count > facilities[ident]['license_limit']:
            license_violations.append({'facility': ident, 'active_beds': count, 'license_limit': facilities[ident]['license_limit']})
    body = {
        'version': 1,
        'day': day,
        'known_at': known_at,
        'catalog_digest': catalog.digest,
        'events_digest': events.digest,
        'beds': beds,
        'quarantined': quarantined,
        'retired': retired,
        'excluded': excluded,
        'license_violations': license_violations,
        'traces': traces,
        'visible_facility_versions': [{'facility': key, 'revision': row['revision']} for key, row in sorted(facilities.items())],
        'publishable': not quarantined and not license_violations,
    }
    return {**body, 'snapshot_digest': fingerprint(body)}


def verify_snapshot(value):
    body = {key: item for key, item in value.items() if key != 'snapshot_digest'}
    if fingerprint(body) != value.get('snapshot_digest'):
        raise ValueError('snapshot checksum mismatch')
    seen = set()
    for partition in ('beds', 'quarantined', 'retired', 'excluded'):
        for row in value[partition]:
            if row['bed'] in seen:
                raise ValueError('bed appears in multiple snapshot partitions')
            seen.add(row['bed'])
    if any(row['category'] not in CATEGORIES for row in value['beds']):
        raise ValueError('unknown active category')
    expected = not value['quarantined'] and not value['license_violations']
    if value['publishable'] != expected:
        raise ValueError('snapshot publishability flag mismatch')
    return True
