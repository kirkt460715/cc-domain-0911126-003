import json
from registry.values import canonical, fingerprint
from bed_events.ledger import IntegrityFailure


def membership_rows(additions):
    result = []
    for table in ('regions', 'facilities', 'events'):
        for row in additions[table]:
            result.append({'table': table, 'id': row['id'], 'revision': row.get('revision', 0), 'digest': fingerprint(row)})
    return sorted(result, key=lambda row: (row['table'], row['id'], row['revision']))


def record_request(ledger, sequence, document, additions):
    ledger.db.execute('INSERT INTO batch_requests VALUES(?,?)', (sequence, canonical(document)))
    for row in membership_rows(additions):
        ledger.db.execute('INSERT INTO batch_members VALUES(?,?,?,?,?)', (sequence, row['table'], row['id'], row['revision'], row['digest']))


def audit_provenance(ledger):
    documents = {}
    members = {}
    for row in ledger.db.execute('SELECT * FROM batch_requests ORDER BY sequence'):
        document = json.loads(row['body'])
        batch = ledger.batch(row['sequence'])
        if fingerprint(document) != batch['request_digest']:
            raise IntegrityFailure('batch request evidence digest mismatch')
        documents[row['sequence']] = document
    for row in ledger.db.execute('SELECT * FROM batch_members ORDER BY sequence,table_name,id,revision'):
        key = row['table_name'], row['id'], row['revision']
        if key in members:
            raise IntegrityFailure('immutable row appears in multiple ingestion batches')
        members[key] = dict(row)
    current = ledger.document()
    expected_keys = set()
    for table in ('regions', 'facilities', 'events'):
        for row in current[table]:
            key = table, row['id'], row.get('revision', 0)
            expected_keys.add(key)
            member = members.get(key)
            if member is None or member['digest'] != fingerprint(row):
                raise IntegrityFailure('ledger row is not anchored in ingestion provenance')
            request = documents.get(member['sequence'])
            if request is None:
                raise IntegrityFailure('provenance member lacks request evidence')
            matches = [item for item in request[table] if item['id'] == key[1] and item.get('revision', 0) == key[2]]
            if len(matches) != 1 or canonical(matches[0]) != canonical(row):
                raise IntegrityFailure('provenance request does not contain immutable row')
    if set(members) != expected_keys:
        raise IntegrityFailure('provenance refers to missing ledger rows')
    batch_count = 0
    for row in ledger.db.execute('SELECT sequence FROM batches ORDER BY sequence'):
        sequence = row['sequence']
        if sequence not in documents:
            raise IntegrityFailure('batch lacks source request evidence')
        batch = ledger.batch(sequence)
        entries = []
        for member in members.values():
            if member['sequence'] == sequence:
                entries.append({'table': member['table_name'], 'id': member['id'], 'revision': member['revision'], 'digest': member['digest']})
        entries.sort(key=lambda item: (item['table'], item['id'], item['revision']))
        if fingerprint(entries) != batch['summary']['membership_digest']:
            raise IntegrityFailure('batch membership digest mismatch')
        for table in ('regions', 'facilities', 'events'):
            actual = sum(member['table'] == table for member in entries)
            if batch['summary'][table]['added'] != actual:
                raise IntegrityFailure('batch added-row count mismatch')
        batch_count += 1
    return {'valid': True, 'batches': batch_count, 'anchored_rows': len(members)}


def lineage_for_bed(ledger, bed):
    document = ledger.document()
    events = [row for row in document['events'] if row['bed'] == bed]
    if not events:
        raise KeyError(bed)
    result = []
    for event in sorted(events, key=lambda row: (row['id'], row['revision'])):
        member = ledger.db.execute("SELECT * FROM batch_members WHERE table_name='events' AND id=? AND revision=?", (event['id'], event['revision'])).fetchone()
        if member is None:
            raise IntegrityFailure('event has no ingestion provenance')
        batch = ledger.batch(member['sequence'])
        result.append({'event': event['id'], 'revision': event['revision'], 'event_digest': member['digest'], 'batch_sequence': member['sequence'], 'batch_request_key': batch['request_key'], 'batch_chain_hash': batch['chain_hash']})
    return {'bed': bed, 'events': result, 'trust_scope': 'local_hash_chain_consistency_without_external_signature'}
