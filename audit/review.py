import json
from registry.values import canonical, fingerprint, identifier, label, integer
from bed_events.ledger import RevisionConflict
from bed_events.schema import parse_package
from indicators.definitions import parse_definition
from indicators.compute import compute
from indicators.disclosure import protect_regions, validate_protection


class Reviews:
    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS indicator_reviews(
            id TEXT PRIMARY KEY,
            snapshot TEXT NOT NULL REFERENCES snapshots(id),
            input_digest TEXT NOT NULL,
            definition TEXT NOT NULL,
            report TEXT NOT NULL,
            report_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('open','approved','rejected')),
            revision INTEGER NOT NULL DEFAULT 1,
            reviewer TEXT,
            reason TEXT
        );
        ''')

    def open(self, ident, snapshot_id, definition_value):
        identifier(ident, 'review.id')
        definition = parse_definition(definition_value)
        with self.ledger.atomic():
            current = self.ledger.get_snapshot(snapshot_id)
            catalog, events = parse_package(current['document'])
            indicator = compute(catalog, current['result'], definition)
            public = protect_regions(catalog, indicator)
            validate_protection(catalog, public)
            body = {'indicator': indicator, 'public': public}
            signature = fingerprint({'snapshot': current['result_digest'], 'definition': definition.export()})
            existing = self.db.execute('SELECT * FROM indicator_reviews WHERE id=?', (ident,)).fetchone()
            if existing:
                if existing['input_digest'] != signature or existing['snapshot'] != snapshot_id:
                    raise RevisionConflict('review identity binds another snapshot or definition')
            else:
                self.db.execute('INSERT INTO indicator_reviews(id,snapshot,input_digest,definition,report,report_digest,state) VALUES(?,?,?,?,?,?,?)', (ident, snapshot_id, signature, canonical(definition.export()), canonical(body), fingerprint(body), 'open'))
        return self.get(ident)

    def get(self, ident):
        row = self.db.execute('SELECT * FROM indicator_reviews WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        value = dict(row)
        value['definition'] = json.loads(value['definition'])
        value['report'] = json.loads(value['report'])
        if fingerprint(value['report']) != value['report_digest']:
            raise ValueError('review report checksum mismatch')
        snapshot = self.ledger.get_snapshot(value['snapshot'])
        expected = fingerprint({'snapshot': snapshot['result_digest'], 'definition': value['definition']})
        if expected != value['input_digest']:
            raise ValueError('review input checksum mismatch')
        return value

    def decide(self, ident, expected_revision, reviewer, approve, reason):
        identifier(reviewer, 'reviewer')
        label(reason, 'reason')
        if not isinstance(approve, bool):
            raise ValueError('approval decision must be boolean')
        integer(expected_revision, 'expected_revision', 1)
        with self.ledger.atomic():
            current = self.get(ident)
            if current['revision'] != expected_revision or current['state'] != 'open':
                raise RevisionConflict('review is stale or already decided')
            if approve and not current['report']['indicator']['quality']['publishable']:
                raise RevisionConflict('cannot approve an unresolved snapshot')
            state = 'approved' if approve else 'rejected'
            self.db.execute('UPDATE indicator_reviews SET state=?,revision=revision+1,reviewer=?,reason=? WHERE id=?', (state, reviewer, reason, ident))
        return self.get(ident)

    def approved_public_report(self, ident):
        value = self.get(ident)
        if value['state'] != 'approved':
            raise RevisionConflict('review has not approved publication')
        return {'review': ident, 'reviewer': value['reviewer'], 'revision': value['revision'], 'report_digest': value['report_digest'], 'public_report': value['report']['public']}

    def audit(self):
        checked = 0
        for row in self.db.execute('SELECT id FROM indicator_reviews ORDER BY id'):
            value = self.get(row['id'])
            current = self.ledger.get_snapshot(value['snapshot'])
            catalog, events = parse_package(current['document'])
            definition = parse_definition(value['definition'])
            indicator = compute(catalog, current['result'], definition)
            expected = {'indicator': indicator, 'public': protect_regions(catalog, indicator)}
            if expected != value['report']:
                raise ValueError('review report replay mismatch')
            if value['state'] == 'approved' and not indicator['quality']['publishable']:
                raise ValueError('approved review has unresolved quality findings')
            if value['state'] != 'open' and (not value['reviewer'] or not value['reason']):
                raise ValueError('decided review lacks reviewer or reason')
            checked += 1
        return {'valid': True, 'reviews': checked}
