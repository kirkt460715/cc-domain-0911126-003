from collections import defaultdict
from datetime import datetime
from registry.values import stamp, fingerprint
from bed_events.replay import replay_bed


def seconds_between(start, end):
    a = datetime.strptime(start, '%Y-%m-%dT%H:%M:%SZ')
    b = datetime.strptime(end, '%Y-%m-%dT%H:%M:%SZ')
    return int((b - a).total_seconds())


def bed_timeline(catalog, events, bed, start, end, known_at):
    stamp(start, 'start')
    stamp(end, 'end')
    stamp(known_at, 'known_at')
    if start >= end:
        raise ValueError('timeline range must have positive duration')
    history = [row for row in events.visible(known_at) if row['bed'] == bed]
    if not any(row['bed'] == bed for row in events.rows):
        raise KeyError(bed)
    boundaries = {start, end}
    for row in history:
        if start < row['effective_at'] < end:
            boundaries.add(row['effective_at'])
        facility = catalog.visible_facility(row['facility'], known_at)
        if facility:
            for key in ('open', 'close'):
                boundary = facility[key] + 'T00:00:00Z'
                if start < boundary < end:
                    boundaries.add(boundary)
    intervals = []
    ordered = sorted(boundaries)
    for left, right in zip(ordered, ordered[1:]):
        selected = [row for row in history if row['effective_at'] <= left]
        replay = replay_bed(selected, right)
        state = replay['state']
        category = None
        facility_id = state['facility'] if state else None
        if replay['quarantined']:
            partition = 'quarantined'
        elif state is None:
            partition = 'not_commissioned'
        elif not state['active']:
            partition = 'retired'
        else:
            facility = catalog.visible_facility(facility_id, known_at)
            day = left[:10]
            if facility is None or not facility['open'] <= day < facility['close']:
                partition = 'outside_facility_validity'
            elif not facility['licensed']:
                partition = 'unlicensed'
            else:
                partition = 'active'
                category = state['category']
        item = {
            'start': left,
            'end': right,
            'seconds': seconds_between(left, right),
            'partition': partition,
            'category': category,
            'facility': facility_id,
            'conflicts': replay['conflicts'],
        }
        if intervals and all(intervals[-1][key] == item[key] for key in ('partition', 'category', 'facility', 'conflicts')):
            intervals[-1]['end'] = right
            intervals[-1]['seconds'] += item['seconds']
        else:
            intervals.append(item)
    duration = seconds_between(start, end)
    if sum(row['seconds'] for row in intervals) != duration:
        raise AssertionError('bed timeline intervals do not partition observation window')
    return {'bed': bed, 'known_at': known_at, 'start': start, 'end': end, 'seconds': duration, 'intervals': intervals}


def exposure(catalog, events, start, end, known_at, definition):
    stamp(start, 'start')
    stamp(end, 'end')
    rows = []
    groups = defaultdict(lambda: {'numerator_seconds': 0, 'denominator_seconds': 0, 'quarantined_seconds': 0})
    for bed in sorted({row['bed'] for row in events.rows}):
        timeline = bed_timeline(catalog, events, bed, start, end, known_at)
        rows.append(timeline)
        for interval in timeline['intervals']:
            if interval['facility'] is None:
                continue
            facility = catalog.visible_facility(interval['facility'], known_at)
            if facility is None or facility['ownership'] not in definition.ownerships:
                continue
            ancestors = catalog.ancestors(facility['region'])
            for region in ancestors:
                group = groups[region]
                if interval['partition'] == 'quarantined':
                    group['quarantined_seconds'] += interval['seconds']
                if interval['partition'] == 'active' and interval['category'] in definition.denominator:
                    group['denominator_seconds'] += interval['seconds']
                    if interval['category'] in definition.numerator:
                        group['numerator_seconds'] += interval['seconds']
    from registry.values import decimal_ratio
    summary = []
    for region in sorted(catalog.region_map):
        value = groups[region]
        summary.append({'region': region, **value, 'exposure_ratio': decimal_ratio(value['numerator_seconds'], value['denominator_seconds'])})
    return {
        'start': start,
        'end': end,
        'known_at': known_at,
        'definition_digest': definition.digest,
        'measure': 'continuous_active_bed_seconds_distinct_from_end_of_day_stock',
        'regions': summary,
        'bed_timelines': rows,
    }
