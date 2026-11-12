import copy
from collections import defaultdict
from registry.values import fingerprint, canonical, stamp
from bed_events.schema import parse_package
from bed_events.ledger import RevisionConflict


def preview(ledger, batch):
    if not isinstance(batch, dict) or set(batch) != {'version', 'regions', 'facilities', 'events'}:
        raise ValueError('preview requires exact import envelope')
    if type(batch['version']) is not int or batch['version'] != 1:
        raise ValueError('preview supports only version 1')
    existing = ledger.document()
    merged = {'version': 1}
    additions = {}
    counts = {}
    for table in ('regions', 'facilities', 'events'):
        if table not in batch or not isinstance(batch[table], list):
            raise ValueError('intake requires all three table lists')
        merged[table], additions[table], unchanged = ledger._merge(existing[table], batch[table], table)
        counts[table] = {'new': len(additions[table]), 'unchanged': unchanged}
    catalog, events = parse_package(merged)
    changed_beds = sorted({row['bed'] for row in additions['events']})
    changed_facilities = sorted({row['id'] for row in additions['facilities']} | {row['facility'] for row in additions['events']})
    return {
        'before_digest': fingerprint(existing),
        'after_digest': fingerprint(merged),
        'counts': counts,
        'changed_beds': changed_beds,
        'changed_facilities': changed_facilities,
        'earliest_changed_effective_at': min((row['effective_at'] for row in additions['events']), default=None),
        'latest_new_knowledge_at': max((row['recorded_at'] for table in ('facilities', 'events') for row in additions[table]), default=None),
        'replay_required_for_semantic_conflicts': True,
    }


def extract_delta(existing_document, complete_document):
    parse_package(existing_document)
    parse_package(complete_document)
    delta = {'version': 1}
    for table in ('regions', 'facilities', 'events'):
        keys = ('id',) if table == 'regions' else ('id', 'revision')
        old = {tuple(row[key] for key in keys): row for row in existing_document[table]}
        new = {tuple(row[key] for key in keys): row for row in complete_document[table]}
        missing = old.keys() - new.keys()
        if missing:
            raise RevisionConflict('complete source removed immutable ledger rows')
        for key in old.keys() & new.keys():
            if canonical(old[key]) != canonical(new[key]):
                raise RevisionConflict('complete source changed immutable ledger row')
        delta[table] = [copy.deepcopy(new[key]) for key in sorted(new.keys() - old.keys())]
    return delta


def knowledge_inventory(catalog, events, known_at):
    stamp(known_at, 'known_at')
    facilities = []
    for ident in sorted(catalog.facility_versions):
        visible = catalog.visible_facility(ident, known_at)
        facilities.append({'facility': ident, 'known': visible is not None, 'revision': visible['revision'] if visible else None})
    visible_events = events.visible(known_at, True)
    by_action = defaultdict(int)
    for row in visible_events:
        by_action[row['action']] += 1
    return {
        'known_at': known_at,
        'facilities': facilities,
        'known_facilities': sum(row['known'] for row in facilities),
        'unknown_facilities': sum(not row['known'] for row in facilities),
        'visible_logical_events': len(visible_events),
        'cancelled_events': sum(row['cancelled'] for row in visible_events),
        'actions': dict(sorted(by_action.items())),
        'future_event_revisions': sum(row['recorded_at'] > known_at for row in events.rows),
    }
