import json
from indicators.definitions import parse_definition
from registry.values import canonical, fingerprint, identifier, integer
from bed_events.ledger import RevisionConflict


class Definitions:
    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS indicator_definitions(
            id TEXT NOT NULL,
            version INTEGER NOT NULL,
            body TEXT NOT NULL,
            digest TEXT NOT NULL,
            PRIMARY KEY(id,version)
        );
        ''')

    def register(self, value):
        definition = parse_definition(value)
        with self.ledger.atomic():
            existing = self.db.execute('SELECT * FROM indicator_definitions WHERE id=? AND version=?', (definition.id, definition.version)).fetchone()
            if existing:
                if existing['digest'] != definition.digest:
                    raise RevisionConflict('indicator version already has another definition')
            else:
                row = self.db.execute('SELECT MAX(version) FROM indicator_definitions WHERE id=?', (definition.id,)).fetchone()
                expected = (row[0] or 0) + 1
                if definition.version != expected:
                    raise RevisionConflict('indicator versions must be contiguous starting at 1')
                self.db.execute('INSERT INTO indicator_definitions VALUES(?,?,?,?)', (definition.id, definition.version, canonical(definition.export()), definition.digest))
        return self.get(definition.id, definition.version)

    def get(self, ident, version):
        identifier(ident, 'definition.id')
        integer(version, 'definition.version', 1)
        row = self.db.execute('SELECT * FROM indicator_definitions WHERE id=? AND version=?', (ident, version)).fetchone()
        if row is None:
            raise KeyError((ident, version))
        definition = parse_definition(json.loads(row['body']))
        if definition.digest != row['digest']:
            raise ValueError('indicator definition integrity failure')
        return {'definition': definition.export(), 'digest': definition.digest}

    def list(self, ident=None):
        if ident is not None:
            identifier(ident, 'definition.id')
            rows = self.db.execute('SELECT id,version FROM indicator_definitions WHERE id=? ORDER BY version', (ident,)).fetchall()
        else:
            rows = self.db.execute('SELECT id,version FROM indicator_definitions ORDER BY id,version').fetchall()
        return [self.get(row['id'], row['version']) for row in rows]

    def changes(self, ident, before, after):
        first = self.get(ident, before)['definition']
        last = self.get(ident, after)['definition']
        if before >= after:
            raise ValueError('definition comparison requires increasing versions')
        changes = []
        for field in ('numerator', 'denominator', 'ownerships', 'target_basis_points', 'small_denominator'):
            if first[field] != last[field]:
                changes.append({'field': field, 'before': first[field], 'after': last[field]})
        comparable = not any(item['field'] in ('numerator', 'denominator', 'ownerships') for item in changes)
        return {'id': ident, 'before_version': before, 'after_version': after, 'same_population_definition': comparable, 'changes': changes}

    def audit(self):
        rows = self.list()
        versions = {}
        for row in rows:
            definition = row['definition']
            versions.setdefault(definition['id'], []).append(definition['version'])
        for ident, values in versions.items():
            if values != list(range(1, len(values) + 1)):
                raise ValueError('stored definition versions are discontinuous')
        return {'valid': True, 'definitions': len(rows), 'families': len(versions)}
