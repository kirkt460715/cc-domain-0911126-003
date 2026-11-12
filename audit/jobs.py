import json
import time
from registry.values import canonical, fingerprint, identifier, integer, iso_day, stamp
from bed_events.ledger import RevisionConflict
from bed_events.schema import parse_package
from bed_events.replay import snapshot


class SnapshotQueue:
    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS snapshot_jobs(
            id TEXT PRIMARY KEY,
            request_digest TEXT NOT NULL,
            document TEXT NOT NULL,
            day TEXT NOT NULL,
            known_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','working','done','failed')),
            generation INTEGER NOT NULL DEFAULT 0,
            owner TEXT,
            deadline INTEGER,
            result TEXT,
            error TEXT
        );
        ''')

    def enqueue(self, ident, day, known_at):
        identifier(ident, 'job.id')
        iso_day(day, 'day')
        stamp(known_at, 'known_at')
        with self.ledger.atomic():
            document = self.ledger.document()
            signature = fingerprint({'document': document, 'day': day, 'known_at': known_at})
            row = self.db.execute('SELECT * FROM snapshot_jobs WHERE id=?', (ident,)).fetchone()
            if row:
                if row['request_digest'] != signature:
                    raise RevisionConflict('snapshot job id already binds another immutable input')
            else:
                self.db.execute('INSERT INTO snapshot_jobs(id,request_digest,document,day,known_at,state) VALUES(?,?,?,?,?,?)',
                                (ident, signature, canonical(document), day, known_at, 'pending'))
        return self.get(ident)

    def get(self, ident):
        row = self.db.execute('SELECT * FROM snapshot_jobs WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        value = dict(row)
        value['document'] = json.loads(value['document'])
        expected = fingerprint({'document': value['document'], 'day': value['day'], 'known_at': value['known_at']})
        if value['request_digest'] != expected:
            raise ValueError('snapshot job input integrity failed')
        if value['state'] == 'working':
            if not value['owner'] or value['deadline'] is None or value['generation'] < 1:
                raise ValueError('working snapshot job has incomplete lease')
        elif value['owner'] is not None or value['deadline'] is not None:
            raise ValueError('inactive snapshot job retains lease')
        if value['state'] == 'done' and value['result'] is None:
            raise ValueError('done snapshot job lacks result')
        if value['state'] != 'done' and value['result'] is not None:
            raise ValueError('unfinished snapshot job contains result')
        if value['state'] == 'failed' and not value['error']:
            raise ValueError('failed snapshot job lacks failure reason')
        if value['state'] != 'failed' and value['error'] is not None:
            raise ValueError('nonfailed snapshot job retains failure reason')
        if value['result'] is not None:
            value['result'] = json.loads(value['result'])
            from bed_events.replay import verify_snapshot
            verify_snapshot(value['result'])
        return value

    def claim(self, ident, owner, now, seconds=60):
        identifier(owner, 'owner')
        integer(now, 'now', 0, 10**15)
        integer(seconds, 'seconds', 1, 3600)
        with self.ledger.atomic():
            current = self.get(ident)
            allowed = current['state'] == 'pending' or current['state'] == 'working' and current['deadline'] <= now
            if not allowed:
                raise RevisionConflict('snapshot job not claimable')
            generation = current['generation'] + 1
            self.db.execute("UPDATE snapshot_jobs SET state='working',generation=?,owner=?,deadline=? WHERE id=?", (generation, owner, now + seconds, ident))
        return {'id': ident, 'owner': owner, 'generation': generation}

    def _fence(self, token, now):
        current = self.get(token['id'])
        if current['state'] != 'working' or current['owner'] != token['owner'] or current['generation'] != token['generation'] or current['deadline'] <= now:
            raise RevisionConflict('snapshot worker lease expired or was replaced')
        return current

    def complete(self, token, result, now):
        with self.ledger.atomic():
            current = self._fence(token, now)
            catalog, events = parse_package(current['document'])
            expected = snapshot(catalog, events, current['day'], current['known_at'])
            if result != expected:
                raise ValueError('worker result does not match immutable snapshot job input')
            self.db.execute("UPDATE snapshot_jobs SET state='done',result=?,owner=NULL,deadline=NULL,error=NULL WHERE id=?", (canonical(result), token['id']))
        return self.get(token['id'])

    def fail(self, token, error, now):
        if not isinstance(error, str) or not error:
            raise ValueError('failure must include an error')
        with self.ledger.atomic():
            self._fence(token, now)
            self.db.execute("UPDATE snapshot_jobs SET state='failed',error=?,owner=NULL,deadline=NULL WHERE id=?", (error[:2000], token['id']))
        return self.get(token['id'])

    def retry(self, ident):
        with self.ledger.atomic():
            current = self.get(ident)
            if current['state'] != 'failed':
                raise RevisionConflict('only failed snapshot jobs can retry')
            self.db.execute("UPDATE snapshot_jobs SET state='pending',error=NULL WHERE id=?", (ident,))
        return self.get(ident)

    def work(self, ident, owner='local', clock=None):
        clock = clock or (lambda: int(time.time()))
        token = self.claim(ident, owner, clock(), 3600)
        current = self.get(ident)
        try:
            catalog, events = parse_package(current['document'])
            result = snapshot(catalog, events, current['day'], current['known_at'])
        except (ValueError, KeyError) as exc:
            self.fail(token, str(exc), clock())
            raise
        return self.complete(token, result, clock())
