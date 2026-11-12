from collections import defaultdict
from registry.values import stamp, iso_day, day_range
from bed_events.replay import snapshot
from indicators.compute import compute, compare_indicators


def compare_snapshots(before, after):
    if before['day'] != after['day']:
        raise ValueError('revision comparison requires same business day')
    partitions = ('beds', 'quarantined', 'retired', 'excluded')
    def index(value):
        result = {}
        for partition in partitions:
            for row in value[partition]:
                result[row['bed']] = {'partition': partition, 'row': row}
        return result
    old, new = index(before), index(after)
    changes = []
    for bed in sorted(old.keys() | new.keys()):
        a, b = old.get(bed), new.get(bed)
        if a == b:
            continue
        if a is None:
            reason = 'newly_known_bed'
        elif b is None:
            reason = 'no_longer_visible_at_day'
        elif a['partition'] != b['partition']:
            reason = 'quality_or_lifecycle_partition_changed'
        else:
            reason = 'category_or_registry_attribution_changed'
        changes.append({'bed': bed, 'reason': reason, 'before': a, 'after': b})
    return {
        'day': before['day'],
        'before_known_at': before['known_at'],
        'after_known_at': after['known_at'],
        'before_digest': before['snapshot_digest'],
        'after_digest': after['snapshot_digest'],
        'changes': changes,
        'before_publishable': before['publishable'],
        'after_publishable': after['publishable'],
    }


def revision_impact(catalog, events, days, before_known, after_known, definition):
    stamp(before_known, 'before_known')
    stamp(after_known, 'after_known')
    if before_known >= after_known:
        raise ValueError('revision impact requires increasing knowledge time')
    if not isinstance(days, list) or len(days) > 3660 or len(days) != len(set(days)):
        raise ValueError('invalid business day selection')
    changes = events.changed_between(before_known, after_known)
    registry_changes = catalog.revisions_between(before_known, after_known)
    reports = []
    for day in sorted(days):
        iso_day(day, 'day')
        before = snapshot(catalog, events, day, before_known)
        after = snapshot(catalog, events, day, after_known)
        delta = compare_snapshots(before, after)
        indicator_delta = compare_indicators(compute(catalog, before, definition), compute(catalog, after, definition))
        reports.append({'day': day, 'beds': delta, 'indicators': indicator_delta})
    return {
        'before_known_at': before_known,
        'after_known_at': after_known,
        'event_revisions': changes,
        'facility_revisions': registry_changes,
        'days': reports,
        'changed_bed_day_cells': sum(len(row['beds']['changes']) for row in reports),
    }


def explain_bed(catalog, events, bed, day, known_at):
    current = snapshot(catalog, events, day, known_at)
    history = [row for row in events.rows if row['bed'] == bed]
    if not history:
        raise KeyError(bed)
    visible = [row for row in events.visible(known_at, True) if row['bed'] == bed]
    state = None
    partition = 'not_effective_or_not_known'
    for key in ('beds', 'quarantined', 'retired', 'excluded'):
        matching = [row for row in current[key] if row['bed'] == bed]
        if matching:
            state = matching[0]
            partition = key
    trace = next((row['trace'] for row in current['traces'] if row['bed'] == bed), [])
    return {
        'bed': bed,
        'day': day,
        'known_at': known_at,
        'partition': partition,
        'current': state,
        'visible_events_including_cancelled': visible,
        'replay_trace': trace,
        'future_knowledge_revisions': sum(row['recorded_at'] > known_at for row in history),
        'snapshot_digest': current['snapshot_digest'],
    }
