import json
from registry.values import canonical, fingerprint, identifier, label, integer
from bed_events.ledger import RevisionConflict


def issues(current):
    result = []
    for row in current['quarantined']:
        for item in row['conflicts']:
            body = {'bed': row['bed'], 'code': item['code'], 'events': item['events'], 'effective_at': item['effective_at']}
            result.append({'issue': fingerprint(body), **body})
    for row in current['license_violations']:
        body = {'facility': row['facility'], 'code': 'license_capacity_exceeded'}
        result.append({'issue': fingerprint(body), **body, 'details': row})
    return result


class Triage:
    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS audit_issues(
            id TEXT PRIMARY KEY,
            issue_key TEXT NOT NULL,
            snapshot TEXT NOT NULL REFERENCES snapshots(id),
            body TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL CHECK(state IN ('open','investigating','resolved')),
            assignee TEXT,
            resolution_snapshot TEXT REFERENCES snapshots(id),
            reason TEXT
        );
        ''')

    def open(self, ident, snapshot_id, issue_key):
        identifier(ident, 'issue.id')
        with self.ledger.atomic():
            current = self.ledger.get_snapshot(snapshot_id)
            matching = [item for item in issues(current['result']) if item['issue'] == issue_key]
            if len(matching) != 1:
                raise ValueError('issue is not present exactly once in snapshot')
            existing = self.db.execute('SELECT * FROM audit_issues WHERE id=?', (ident,)).fetchone()
            if existing:
                if existing['snapshot'] != snapshot_id or existing['issue_key'] != issue_key:
                    raise RevisionConflict('issue id already binds another finding')
            else:
                self.db.execute('INSERT INTO audit_issues(id,issue_key,snapshot,body,state) VALUES(?,?,?,?,?)', (ident, issue_key, snapshot_id, canonical(matching[0]), 'open'))
        return self.get(ident)

    def get(self, ident):
        row = self.db.execute('SELECT * FROM audit_issues WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        value = dict(row)
        value['body'] = json.loads(value['body'])
        original = self.ledger.get_snapshot(value['snapshot'])
        matching = [item for item in issues(original['result']) if item['issue'] == value['issue_key']]
        if matching != [value['body']]:
            raise ValueError('triage finding no longer matches original snapshot')
        return value

    def assign(self, ident, revision, assignee):
        identifier(assignee, 'assignee')
        with self.ledger.atomic():
            value = self.get(ident)
            if value['revision'] != revision or value['state'] == 'resolved':
                raise RevisionConflict('issue is stale or resolved')
            self.db.execute("UPDATE audit_issues SET state='investigating',assignee=?,revision=revision+1 WHERE id=?", (assignee, ident))
        return self.get(ident)

    def resolve(self, ident, revision, resolution_snapshot, reason):
        label(reason, 'reason')
        with self.ledger.atomic():
            value = self.get(ident)
            if value['revision'] != revision or value['state'] != 'investigating':
                raise RevisionConflict('issue must be current and investigating')
            before = self.ledger.get_snapshot(value['snapshot'])
            after = self.ledger.get_snapshot(resolution_snapshot)
            if before['day'] != after['day'] or before['known_at'] >= after['known_at']:
                raise ValueError('resolution requires same business day and later knowledge')
            if any(item['issue'] == value['issue_key'] for item in issues(after['result'])):
                raise RevisionConflict('finding still present in resolution snapshot')
            if not after['result']['publishable']:
                raise RevisionConflict('resolution snapshot must have no unresolved bed or license conflicts')
            self.db.execute("UPDATE audit_issues SET state='resolved',resolution_snapshot=?,reason=?,revision=revision+1 WHERE id=?", (resolution_snapshot, reason, ident))
        return self.get(ident)

    def audit(self):
        count = 0
        for row in self.db.execute('SELECT id FROM audit_issues ORDER BY id'):
            value = self.get(row['id'])
            if value['state'] == 'resolved':
                resolution = self.ledger.get_snapshot(value['resolution_snapshot'])
                if not resolution['result']['publishable'] or not value['reason'] or not value['assignee']:
                    raise ValueError('resolved issue lacks clean evidence or attribution')
                if any(item['issue'] == value['issue_key'] for item in issues(resolution['result'])):
                    raise ValueError('resolved issue remains in evidence')
            count += 1
        return {'valid': True, 'issues': count}
