from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import uuid
from registry.values import canonical, fingerprint, identifier, stamp, integer, DataError
from bed_events.schema import parse_package
from bed_events.replay import snapshot, verify_snapshot


class RevisionConflict(ValueError):
    pass


class IntegrityFailure(ValueError):
    pass


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS regions(id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS facilities(
            id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            body TEXT NOT NULL,
            digest TEXT NOT NULL,
            PRIMARY KEY(id,revision)
        );
        CREATE TABLE IF NOT EXISTS events(
            id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            bed TEXT NOT NULL,
            facility TEXT NOT NULL,
            body TEXT NOT NULL,
            digest TEXT NOT NULL,
            PRIMARY KEY(id,revision)
        );
        CREATE INDEX IF NOT EXISTS events_knowledge ON events(recorded_at,id,revision);
        CREATE INDEX IF NOT EXISTS events_bed ON events(bed,effective_at);
        CREATE TABLE IF NOT EXISTS batches(
            sequence INTEGER PRIMARY KEY,
            request_key TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            chain_hash TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batch_requests(sequence INTEGER PRIMARY KEY REFERENCES batches(sequence),body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batch_members(
            sequence INTEGER NOT NULL REFERENCES batches(sequence),
            table_name TEXT NOT NULL,
            id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            digest TEXT NOT NULL,
            PRIMARY KEY(table_name,id,revision)
        );
        CREATE TABLE IF NOT EXISTS snapshots(
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            known_at TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            document TEXT NOT NULL,
            result TEXT NOT NULL,
            result_digest TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS publications(
            label TEXT PRIMARY KEY,
            snapshot TEXT NOT NULL REFERENCES snapshots(id),
            digest TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        ''')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def atomic(self):
        nested = self.db.in_transaction
        savepoint = 'bed_' + uuid.uuid4().hex
        if nested:
            self.db.execute('SAVEPOINT ' + savepoint)
        else:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            if nested:
                self.db.execute('ROLLBACK TO ' + savepoint)
                self.db.execute('RELEASE ' + savepoint)
            else:
                self.db.execute('ROLLBACK')
            raise
        else:
            self.db.execute('RELEASE ' + savepoint if nested else 'COMMIT')

    def now(self):
        return datetime.now(timezone.utc).isoformat()

    def _read(self, table):
        if table not in ('regions', 'facilities', 'events'):
            raise ValueError('unsupported ledger table')
        order = 'id' if table == 'regions' else 'id,revision'
        result = []
        for row in self.db.execute(f'SELECT * FROM {table} ORDER BY {order}'):
            body = json.loads(row['body'])
            if fingerprint(body) != row['digest']:
                raise IntegrityFailure('ledger body checksum mismatch: ' + table)
            result.append(body)
        return result

    def document(self):
        return {'version': 1, 'regions': self._read('regions'), 'facilities': self._read('facilities'), 'events': self._read('events')}

    def _merge(self, existing, incoming, table):
        result = list(existing)
        keys = ('id',) if table == 'regions' else ('id', 'revision')
        indexed = {tuple(row[key] for key in keys): row for row in existing}
        seen = set()
        added = []
        unchanged = 0
        for row in incoming:
            if not isinstance(row, dict) or any(key not in row for key in keys):
                raise DataError(table, 'row does not have its identity fields')
            key = tuple(row[key] for key in keys)
            if key in seen:
                raise RevisionConflict('duplicate identity in submitted batch')
            seen.add(key)
            if key in indexed:
                if canonical(indexed[key]) != canonical(row):
                    raise RevisionConflict('immutable revision changed: ' + str(key))
                unchanged += 1
            else:
                result.append(row)
                added.append(row)
        return result, added, unchanged

    def ingest(self, request_key, document):
        identifier(request_key, 'request_key')
        if not isinstance(document, dict) or set(document) != {'version', 'regions', 'facilities', 'events'}:
            raise DataError('$', 'batch must have version, regions, facilities and events')
        if type(document['version']) is not int or document['version'] != 1:
            raise DataError('version', 'only version 1 supported')
        for table in ('regions', 'facilities', 'events'):
            if not isinstance(document[table], list) or len(document[table]) > 100000:
                raise DataError(table, 'expected bounded row list')
        request_digest = fingerprint(document)
        with self.atomic():
            existing = self.db.execute('SELECT * FROM batches WHERE request_key=?', (request_key,)).fetchone()
            if existing:
                if existing['request_digest'] != request_digest:
                    raise RevisionConflict('request key has different payload')
                return self.batch(existing['sequence'])
            prior = self.document()
            merged = {'version': 1}
            additions = {}
            summary = {}
            for table in ('regions', 'facilities', 'events'):
                merged[table], additions[table], unchanged = self._merge(prior[table], document[table], table)
                summary[table] = {'added': len(additions[table]), 'unchanged': unchanged}
            parse_package(merged)
            from audit.provenance import membership_rows, record_request
            summary['membership_digest'] = fingerprint(membership_rows(additions))
            for row in additions['regions']:
                self.db.execute('INSERT INTO regions VALUES(?,?,?)', (row['id'], canonical(row), fingerprint(row)))
            for row in additions['facilities']:
                self.db.execute('INSERT INTO facilities VALUES(?,?,?,?,?)', (row['id'], row['revision'], row['recorded_at'], canonical(row), fingerprint(row)))
            for row in additions['events']:
                self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)', (row['id'], row['revision'], row['recorded_at'], row['effective_at'], row['bed'], row['facility'], canonical(row), fingerprint(row)))
            last = self.db.execute('SELECT sequence,chain_hash FROM batches ORDER BY sequence DESC LIMIT 1').fetchone()
            sequence = last['sequence'] + 1 if last else 1
            previous = last['chain_hash'] if last else '0' * 64
            created = self.now()
            record = {'sequence': sequence, 'request_key': request_key, 'request_digest': request_digest, 'previous_hash': previous, 'summary': summary, 'created_at': created}
            chain = fingerprint(record)
            self.db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?)', (sequence, request_key, request_digest, previous, chain, canonical(summary), created))
            record_request(self, sequence, document, additions)
        return self.batch(sequence)

    def batch(self, sequence):
        row = self.db.execute('SELECT * FROM batches WHERE sequence=?', (sequence,)).fetchone()
        if row is None:
            raise KeyError(sequence)
        value = dict(row)
        value['summary'] = json.loads(value['summary'])
        body = {key: item for key, item in value.items() if key != 'chain_hash'}
        if fingerprint(body) != value['chain_hash']:
            raise IntegrityFailure('batch chain row checksum mismatch')
        return value

    def history(self, after=0, limit=100):
        integer(after, 'after')
        integer(limit, 'limit', 1, 1000)
        return [self.batch(row['sequence']) for row in self.db.execute('SELECT sequence FROM batches WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit))]

    def create_snapshot(self, ident, day, known_at):
        identifier(ident, 'snapshot.id')
        with self.atomic():
            document = self.document()
            catalog, events = parse_package(document)
            result = snapshot(catalog, events, day, known_at)
            input_digest = fingerprint(document)
            existing = self.db.execute('SELECT * FROM snapshots WHERE id=?', (ident,)).fetchone()
            if existing:
                if existing['input_digest'] != input_digest or existing['day'] != day or existing['known_at'] != known_at:
                    raise RevisionConflict('snapshot id already binds another input or cutoff')
                return self.get_snapshot(ident)
            self.db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)', (ident, day, known_at, input_digest, canonical(document), canonical(result), result['snapshot_digest'], self.now()))
        return self.get_snapshot(ident)

    def get_snapshot(self, ident):
        row = self.db.execute('SELECT * FROM snapshots WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        value = dict(row)
        value['document'] = json.loads(value['document'])
        value['result'] = json.loads(value['result'])
        if fingerprint(value['document']) != value['input_digest']:
            raise IntegrityFailure('snapshot input checksum mismatch')
        verify_snapshot(value['result'])
        if value['result']['snapshot_digest'] != value['result_digest']:
            raise IntegrityFailure('snapshot result checksum mismatch')
        return value

    def publish(self, label, ident):
        identifier(label, 'label')
        with self.atomic():
            value = self.get_snapshot(ident)
            if not value['result']['publishable']:
                raise RevisionConflict('snapshot has quarantined beds or license violations')
            existing = self.db.execute('SELECT * FROM publications WHERE label=?', (label,)).fetchone()
            if existing:
                if existing['snapshot'] != ident or existing['digest'] != value['result_digest']:
                    raise RevisionConflict('publication labels are immutable')
            else:
                self.db.execute('INSERT INTO publications VALUES(?,?,?,?)', (label, ident, value['result_digest'], self.now()))
        return self.publication(label)

    def publication(self, label):
        row = self.db.execute('SELECT * FROM publications WHERE label=?', (label,)).fetchone()
        if row is None:
            raise KeyError(label)
        value = self.get_snapshot(row['snapshot'])
        if row['digest'] != value['result_digest'] or not value['result']['publishable']:
            raise IntegrityFailure('publication does not match publishable snapshot')
        return {**dict(row), 'snapshot_data': value}

    def audit(self, replay=True):
        if [row[0] for row in self.db.execute('PRAGMA integrity_check')] != ['ok']:
            raise IntegrityFailure('SQLite integrity check failed')
        if self.db.execute('PRAGMA foreign_key_check').fetchall():
            raise IntegrityFailure('SQLite foreign key check failed')
        document = self.document()
        parse_package(document)
        previous = '0' * 64
        sequence = 0
        for row in self.db.execute('SELECT sequence FROM batches ORDER BY sequence'):
            current = self.batch(row['sequence'])
            if current['sequence'] != sequence + 1 or current['previous_hash'] != previous:
                raise IntegrityFailure('batch hash chain is discontinuous')
            sequence = current['sequence']
            previous = current['chain_hash']
        snapshots = 0
        for row in self.db.execute('SELECT id FROM snapshots ORDER BY id'):
            value = self.get_snapshot(row['id'])
            if replay:
                catalog, events = parse_package(value['document'])
                reconstructed = snapshot(catalog, events, value['day'], value['known_at'])
                if reconstructed != value['result']:
                    raise IntegrityFailure('snapshot replay mismatch')
            snapshots += 1
        publications = 0
        for row in self.db.execute('SELECT label FROM publications ORDER BY label'):
            self.publication(row['label'])
            publications += 1
        from audit.provenance import audit_provenance
        audit_provenance(self)
        table_names = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'staged_imports' in table_names:
            from bed_events.staging import Staging
            Staging(self).audit()
        if 'indicator_reviews' in table_names:
            from audit.review import Reviews
            Reviews(self).audit()
        if 'audit_issues' in table_names:
            from audit.triage import Triage
            Triage(self).audit()
        if 'indicator_definitions' in table_names:
            from indicators.registry import Definitions
            Definitions(self).audit()
        if 'snapshot_jobs' in table_names:
            from audit.jobs import SnapshotQueue
            queue = SnapshotQueue(self)
            for job_row in self.db.execute('SELECT id FROM snapshot_jobs ORDER BY id'):
                job = queue.get(job_row['id'])
                if job['state'] == 'done':
                    job_catalog, job_events = parse_package(job['document'])
                    if snapshot(job_catalog, job_events, job['day'], job['known_at']) != job['result']:
                        raise IntegrityFailure('snapshot job replay mismatch')
        return {'valid': True, 'batches': sequence, 'snapshots': snapshots, 'publications': publications, 'document_digest': fingerprint(document), 'head_hash': previous}
