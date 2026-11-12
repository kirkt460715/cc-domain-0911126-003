import copy
import json
from registry.values import canonical, fingerprint, identifier, integer
from bed_events.schema import parse_package
from bed_events.ledger import RevisionConflict
from registry.intake import preview


class Staging:
    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS staged_imports(
            id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            body TEXT NOT NULL,
            digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('draft','promoted')),
            batch_sequence INTEGER,
            error TEXT
        );
        ''')

    def put(self, ident, document, expected_revision=None):
        identifier(ident, 'stage.id')
        if not isinstance(document, dict) or set(document) != {'version', 'regions', 'facilities', 'events'}:
            raise ValueError('stage requires full import envelope')
        if type(document['version']) is not int or document['version'] != 1:
            raise ValueError('unsupported stage version')
        for table in ('regions', 'facilities', 'events'):
            if not isinstance(document[table], list) or len(document[table]) > 100000:
                raise ValueError('stage tables must be bounded lists')
        signature = fingerprint(document)
        with self.ledger.atomic():
            row = self.db.execute('SELECT * FROM staged_imports WHERE id=?', (ident,)).fetchone()
            if row is None:
                if expected_revision is not None:
                    raise RevisionConflict('cannot replace absent stage')
                self.db.execute('INSERT INTO staged_imports(id,revision,body,digest,state) VALUES(?,?,?,?,?)', (ident, 1, canonical(document), signature, 'draft'))
            else:
                if row['state'] != 'draft':
                    raise RevisionConflict('promoted stage is immutable')
                if row['revision'] != expected_revision:
                    raise RevisionConflict('stale stage revision')
                self.db.execute('UPDATE staged_imports SET revision=revision+1,body=?,digest=?,error=NULL WHERE id=?', (canonical(document), signature, ident))
        return self.get(ident)

    def get(self, ident):
        row = self.db.execute('SELECT * FROM staged_imports WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        value = dict(row)
        value['body'] = json.loads(value['body'])
        if fingerprint(value['body']) != value['digest']:
            raise ValueError('staged import checksum mismatch')
        return value

    def combine(self, selections):
        if not isinstance(selections, dict) or not selections or len(selections) > 100:
            raise ValueError('select 1..100 stage revisions')
        result = {'version': 1, 'regions': [], 'facilities': [], 'events': []}
        indexed = {table: {} for table in ('regions', 'facilities', 'events')}
        for ident, revision in sorted(selections.items()):
            integer(revision, 'stage.revision', 1)
            stage = self.get(ident)
            if stage['revision'] != revision or stage['state'] != 'draft':
                raise RevisionConflict('stage selection is stale or promoted')
            for table in indexed:
                keys = ('id',) if table == 'regions' else ('id', 'revision')
                for row in stage['body'][table]:
                    if not isinstance(row, dict) or any(key not in row for key in keys):
                        raise ValueError('staged row lacks identity')
                    key = tuple(row[field] for field in keys)
                    if key in indexed[table]:
                        if canonical(row) != canonical(indexed[table][key]):
                            raise RevisionConflict('stages disagree about an immutable revision')
                    else:
                        indexed[table][key] = row
                        result[table].append(copy.deepcopy(row))
        return result

    def inspect(self, selections):
        document = self.combine(selections)
        result = preview(self.ledger, document)
        return {**result, 'selected_revisions': dict(selections), 'combined_digest': fingerprint(document)}

    def promote(self, selections, request_key, expected_combined_digest):
        identifier(request_key, 'request_key')
        try:
            with self.ledger.atomic():
                document = self.combine(selections)
                if fingerprint(document) != expected_combined_digest:
                    raise RevisionConflict('staged inputs changed after review')
                preview(self.ledger, document)
                result = self.ledger.ingest(request_key, document)
                for ident in selections:
                    self.db.execute("UPDATE staged_imports SET state='promoted',batch_sequence=?,error=NULL WHERE id=?", (result['sequence'], ident))
        except (ValueError, KeyError) as exc:
            with self.ledger.atomic():
                for ident in selections:
                    self.db.execute("UPDATE staged_imports SET error=? WHERE id=? AND state='draft'", (str(exc)[:2000], ident))
            raise
        return {'batch': result, 'stages': [self.get(ident) for ident in sorted(selections)]}

    def pending(self):
        return [self.get(row['id']) for row in self.db.execute("SELECT id FROM staged_imports WHERE state='draft' ORDER BY id")]

    def audit(self):
        rows = [self.get(row['id']) for row in self.db.execute('SELECT id FROM staged_imports ORDER BY id')]
        for row in rows:
            if row['state'] == 'promoted':
                if row['batch_sequence'] is None:
                    raise ValueError('promoted stage lacks batch receipt')
                self.ledger.batch(row['batch_sequence'])
            elif row['batch_sequence'] is not None:
                raise ValueError('draft stage has batch receipt')
        return {'valid': True, 'drafts': sum(row['state'] == 'draft' for row in rows), 'promoted': sum(row['state'] == 'promoted' for row in rows)}
