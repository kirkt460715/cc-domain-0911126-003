import copy
import hashlib
import http.client
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
from test_event_history import data, DAY, EARLY, LATEST, FIXTURE
from registry.values import canonical, fingerprint
from registry.intake import preview
from bed_events.schema import parse_package
from bed_events.replay import snapshot
from bed_events.ledger import Ledger, RevisionConflict
from bed_events.tabular import read_table, write_table, read_directory, write_directory
from indicators.definitions import nursing_definition
from indicators.compute import measures
from audit.http import make_server
from audit.service import Service
from audit.package import write_package, verify_package
from audit.query import beds, decode_cursor, encode_cursor, facility_drilldown, partition_summary
from audit.triage import Triage, issues
from bed_audit import demo, parser, dispatch


class DeliveryProtocols(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / 'registry.sqlite'
        self.document = data()
        self.catalog, self.events = parse_package(self.document)
        self.current = snapshot(self.catalog, self.events, DAY, LATEST)
        self.definition = nursing_definition()

    def tearDown(self):
        self.temporary.cleanup()

    def test_csv_roundtrip_preserves_every_revision_and_boolean(self):
        directory = self.root / 'tables'
        outcome = write_directory(self.document, directory)
        restored = read_directory(directory)
        self.assertEqual(outcome['files'], ['events.csv', 'facilities.csv', 'regions.csv'])
        self.assertEqual(restored, self.document)
        self.assertIs(restored['events'][0]['cancelled'], False)
        self.assertIsNone(restored['events'][0]['expected'])
        self.assertEqual(restored['events'][7]['revision'], 2)
        with self.assertRaises(FileExistsError):
            write_directory(self.document, directory)

    def test_csv_bad_row_width_headers_and_explicit_types(self):
        with self.assertRaises(ValueError):
            read_table('id,parent,name\na,,A,extra\n', 'regions')
        with self.assertRaises(ValueError):
            read_table('id,id,name\na,a,A\n', 'regions')
        encoded = write_table(self.document, 'facilities')
        with self.assertRaisesRegex(ValueError, 'boolean'):
            read_table(encoded.replace(',true,', ',1,'), 'facilities')
        encoded = write_table(self.document, 'events')
        with self.assertRaisesRegex(ValueError, 'decimal integer'):
            read_table(encoded.replace(',1,', ',1.0,', 1), 'events')
        with self.assertRaises(ValueError):
            read_table(encoded, 'unknown')

    def test_csv_directory_has_exact_regular_file_set(self):
        directory = self.root / 'tables'
        write_directory(self.document, directory)
        extra = directory / 'extra.csv'
        extra.write_text('x')
        with self.assertRaisesRegex(ValueError, 'file set'):
            read_directory(directory)
        extra.unlink()
        source = self.root / 'external.csv'
        path = directory / 'regions.csv'
        source.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(source)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            read_directory(directory)

    def test_audit_package_replays_all_derived_files(self):
        directory = self.root / 'evidence'
        written = write_package(self.document, self.current, self.definition, directory)
        self.assertEqual(written['files'], 10)
        self.assertTrue(written['verified'])
        self.assertEqual(verify_package(directory), written)
        with self.assertRaises(FileExistsError):
            write_package(self.document, self.current, self.definition, directory)
        path = directory / 'active-beds.csv'
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            verify_package(directory)

    def test_recomputed_file_hash_does_not_hide_wrong_derived_report(self):
        directory = self.root / 'evidence'
        write_package(self.document, self.current, self.definition, directory)
        path = directory / 'quality.json'
        path.write_text('{}')
        manifest_path = directory / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['quality.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(canonical(manifest))
        with self.assertRaisesRegex(ValueError, 'derived audit'):
            verify_package(directory)
        extra = directory / 'unexpected'
        extra.write_text('x')
        with self.assertRaisesRegex(ValueError, 'file set'):
            verify_package(directory)

    def test_bed_query_paging_and_filter_context(self):
        rows = []
        cursor = None
        while True:
            page = beds(self.current, columns=['bed', 'category'], limit=2, cursor=cursor)
            rows.extend(page['rows'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(len(rows), 5)
        self.assertEqual(len({row['bed'] for row in rows}), 5)
        self.assertEqual([row['bed'] for row in rows], sorted(row['bed'] for row in rows))
        selected = beds(self.current, {'category': ['nursing']}, ['bed'])
        self.assertEqual({row['bed'] for row in selected['rows']}, {'bed-2', 'bed-4'})
        self.assertIsNone(selected['next_cursor'])

    def test_cursor_is_bound_to_snapshot_and_projection(self):
        first = beds(self.current, columns=['bed'], limit=1)
        cursor = first['next_cursor']
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            beds(self.current, columns=['category'], cursor=cursor)
        early = snapshot(self.catalog, self.events, DAY, EARLY)
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            beds(early, columns=['bed'], cursor=cursor)
        token = decode_cursor(cursor)
        token['offset'] = 99
        with self.assertRaises(ValueError):
            beds(self.current, columns=['bed'], cursor=encode_cursor(token))
        with self.assertRaises(ValueError):
            beds(self.current, cursor='%%invalid')

    def test_query_and_facility_drilldown_count_conservation(self):
        f1 = facility_drilldown(self.catalog, self.current, 'f1')
        self.assertEqual(sum(f1['category_counts'].values()), 3)
        self.assertEqual(f1['license_headroom'], 7)
        self.assertTrue(f1['effective_on_day'])
        partitions = partition_summary(self.current)
        self.assertEqual(partitions['partition_total'], 5)
        self.assertEqual(partitions['traced_beds'], 5)
        invalid = [{'filters': {'unknown': []}}, {'columns': ['bed', 'bed']}, {'limit': 0}]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                beds(self.current, **kwargs)

    def test_conversion_gap_matches_independent_integer_search(self):
        rng = random.Random(4242)
        for _ in range(100):
            denominator = rng.randint(1, 100)
            numerator = rng.randint(0, denominator)
            measured = measures(numerator, denominator, self.definition)
            expected_conversion = next(count for count in range(denominator - numerator + 1) if (numerator + count) * 10000 >= denominator * 7300)
            expected_addition = next(count for count in range(1000) if (numerator + count) * 10000 >= (denominator + count) * 7300)
            with self.subTest(n=numerator, d=denominator):
                self.assertEqual(measured['conversion_gap'], expected_conversion)
                self.assertEqual(measured['additional_nursing_gap'], expected_addition)

    def test_triage_resolution_requires_later_clean_same_day_evidence(self):
        broken = copy.deepcopy(self.document)
        conflict = {**broken['events'][6], 'id': 'competing'}
        broken['events'].append(conflict)
        with Ledger(self.path) as ledger:
            ledger.ingest('broken-import', broken)
            prior = ledger.create_snapshot('broken', DAY, LATEST)
            triage = Triage(ledger)
            issue = issues(prior['result'])[0]['issue']
            opened = triage.open('case', 'broken', issue)
            self.assertEqual(opened['state'], 'open')
            assigned = triage.assign('case', 1, 'analyst')
            self.assertEqual(assigned['revision'], 2)
            ledger.create_snapshot('broken-later', DAY, '2026-11-10T01:00:00Z')
            with self.assertRaises(RevisionConflict):
                triage.resolve('case', 2, 'broken-later', 'still unresolved')
            correction = {**conflict, 'revision': 2, 'recorded_at': '2026-11-11T00:00:00Z', 'cancelled': True}
            delta = {'version': 1, 'regions': [], 'facilities': [], 'events': [correction]}
            ledger.ingest('correction', delta)
            ledger.create_snapshot('fixed', DAY, '2026-11-12T00:00:00Z')
            resolved = triage.resolve('case', 2, 'fixed', 'duplicate source event cancelled')
            self.assertEqual(resolved['state'], 'resolved')
            self.assertEqual(resolved['revision'], 3)
            self.assertTrue(triage.audit()['valid'])
            self.assertTrue(ledger.audit()['valid'])

    def test_service_enforces_fields_and_immutable_snapshot_queries(self):
        service = Service(self.path)
        receipt = service.handle('POST', '/ingest', {'request_key': 'import', 'document': self.document})
        self.assertEqual(receipt['sequence'], 1)
        service.handle('POST', '/snapshot', {'id': 'current', 'day': DAY, 'known_at': LATEST})
        value = service.handle('POST', '/indicator', {'snapshot': 'current'})
        self.assertEqual(next(row for row in value['rows'] if row['region'] == 'county')['denominator'], 5)
        public = service.handle('POST', '/indicator', {'snapshot': 'current', 'protect': True})
        self.assertTrue(all(row['suppressed'] for row in public['rows']))
        with self.assertRaises(ValueError):
            service.handle('POST', '/snapshot', {'id': 'wrong', 'day': DAY})
        with self.assertRaises(LookupError):
            service.handle('POST', '/missing', {})
        self.assertTrue(service.handle('GET', '/health', {})['ok'])

    def test_cli_demo_and_replay_commands_use_real_business_data(self):
        result = demo(FIXTURE)
        self.assertEqual(result['revision_impact'], 1)
        self.assertEqual(result['job_state'], 'done')
        self.assertTrue(result['restored']['valid'])
        self.assertTrue(result['evidence']['verified'])
        args = parser().parse_args(['period', '--fixture', str(FIXTURE), '--start', DAY, '--end', '2026-11-06'])
        period = dispatch(args)
        self.assertEqual(period['days'], 5)
        args = parser().parse_args(['stock-bridge', '--fixture', str(FIXTURE), '--start', DAY, '--end', '2026-11-05'])
        self.assertTrue(dispatch(args)['quality_resolved'])


class HTTPProtocols(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'http.sqlite'
        self.server = make_server(self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        payload = canonical(body).encode() if body is not None else None
        request_headers = {'Content-Type': 'application/json'}
        request_headers.update(headers or {})
        try:
            connection.request(method, path, payload, request_headers)
            response = connection.getresponse()
            content = response.read()
            return response.status, json.loads(content)
        finally:
            connection.close()

    def test_http_import_snapshot_publish_and_filtered_report(self):
        status, imported = self.request('POST', '/ingest', {'request_key': 'http-import', 'document': data()})
        self.assertEqual(status, 200)
        self.assertEqual(imported['sequence'], 1)
        status, current = self.request('POST', '/snapshot', {'id': 'current', 'day': DAY, 'known_at': LATEST})
        self.assertEqual(status, 200)
        self.assertEqual(len(current['result']['beds']), 5)
        status, published = self.request('POST', '/publish', {'label': 'public', 'snapshot': 'current'})
        self.assertEqual(status, 200)
        self.assertEqual(published['snapshot'], 'current')
        status, report = self.request('POST', '/beds', {'snapshot': 'current', 'filters': {'category': ['nursing']}, 'columns': ['bed']})
        self.assertEqual(status, 200)
        self.assertEqual(report['total'], 2)
        status, health = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertTrue(health['ok'])

    def test_http_conflict_and_missing_resources_have_distinct_status(self):
        self.request('POST', '/ingest', {'request_key': 'same', 'document': data()})
        changed = data()
        changed['events'].pop()
        status, value = self.request('POST', '/ingest', {'request_key': 'same', 'document': changed})
        self.assertEqual(status, 409)
        self.assertIn('different payload', value['error'])
        status, value = self.request('POST', '/publication', {'label': 'absent'})
        self.assertEqual(status, 404)
        status, value = self.request('POST', '/snapshot', {'id': 'missing-fields'})
        self.assertEqual(status, 400)
        status, value = self.request('PUT', '/snapshot', {})
        self.assertEqual(status, 405)

    def test_http_rejects_unsupported_media_query_and_oversized_length(self):
        status, value = self.request('POST', '/audit', {}, {'Content-Type': 'text/plain'})
        self.assertEqual(status, 400)
        status, value = self.request('GET', '/health?extra=1')
        self.assertEqual(status, 400)
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.putrequest('POST', '/ingest')
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', '10000001')
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            self.assertIn('too large', json.loads(response.read())['error'])
        finally:
            connection.close()

    def test_http_service_supports_period_exposure_and_versioned_reviews(self):
        self.request('POST', '/ingest', {'request_key': 'http-import', 'document': data()})
        self.request('POST', '/snapshot', {'id': 'current', 'day': DAY, 'known_at': LATEST})
        status, reviewed = self.request('POST', '/reviews/open', {'id': 'review', 'snapshot': 'current'})
        self.assertEqual(status, 200)
        self.assertEqual(reviewed['state'], 'open')
        status, decision = self.request('POST', '/reviews/decide', {'id': 'review', 'expected_revision': 1, 'reviewer': 'analyst', 'approve': True, 'reason': 'verified synthetic sample'})
        self.assertEqual(status, 200)
        self.assertEqual(decision['state'], 'approved')
        status, public = self.request('POST', '/reviews/public', {'id': 'review'})
        self.assertEqual(status, 200)
        self.assertEqual(public['reviewer'], 'analyst')
        status, period = self.request('POST', '/period', {'start': DAY, 'end': '2026-11-06', 'known_at': LATEST})
        self.assertEqual(status, 200)
        self.assertEqual(period['days'], 5)

    def test_http_staging_combines_and_promotes_frozen_revisions(self):
        status, staged = self.request('POST', '/stages/put', {'id': 'source', 'document': data()})
        self.assertEqual(status, 200)
        self.assertEqual(staged['revision'], 1)
        status, inspected = self.request('POST', '/stages/inspect', {'selections': {'source': 1}})
        self.assertEqual(status, 200)
        self.assertEqual(inspected['counts']['events']['new'], 9)
        status, promoted = self.request('POST', '/stages/promote', {'selections': {'source': 1}, 'request_key': 'staged', 'expected_digest': inspected['combined_digest']})
        self.assertEqual(status, 200)
        self.assertEqual(promoted['batch']['sequence'], 1)
        self.assertEqual(promoted['stages'][0]['state'], 'promoted')
        status, value = self.request('POST', '/stages/put', {'id': 'source', 'document': data(), 'expected_revision': 1})
        self.assertEqual(status, 409)
        status, pending = self.request('POST', '/stages/pending', {})
        self.assertEqual(status, 200)
        self.assertEqual(pending['stages'], [])
        status, provenance = self.request('POST', '/provenance/bed', {'bed': 'bed-1'})
        self.assertEqual(status, 200)
        self.assertEqual(len(provenance['events']), 3)

    def test_http_snapshot_job_cannot_be_rebound_or_retried_after_done(self):
        self.request('POST', '/ingest', {'request_key': 'import', 'document': data()})
        status, job = self.request('POST', '/jobs/enqueue', {'id': 'job', 'day': DAY, 'known_at': LATEST})
        self.assertEqual(status, 200)
        self.assertEqual(job['state'], 'pending')
        status, conflict = self.request('POST', '/jobs/enqueue', {'id': 'job', 'day': '2026-11-02', 'known_at': LATEST})
        self.assertEqual(status, 409)
        status, complete = self.request('POST', '/jobs/work', {'id': 'job'})
        self.assertEqual(status, 200)
        self.assertEqual(complete['state'], 'done')
        status, observed = self.request('POST', '/jobs/get', {'id': 'job'})
        self.assertEqual(status, 200)
        self.assertEqual(observed['result'], complete['result'])
        status, rejected = self.request('POST', '/jobs/retry', {'id': 'job'})
        self.assertEqual(status, 409)
        status, audited = self.request('POST', '/audit', {})
        self.assertEqual(status, 200)
        self.assertTrue(audited['valid'])

    def test_http_definition_versions_and_compatibility_report(self):
        definition = nursing_definition().export()
        status, first = self.request('POST', '/definitions/register', {'definition': definition})
        self.assertEqual(status, 200)
        alternative = {**definition, 'version': 2, 'target_basis_points': 8000}
        status, second = self.request('POST', '/definitions/register', {'definition': alternative})
        self.assertEqual(status, 200)
        self.assertNotEqual(first['digest'], second['digest'])
        status, changes = self.request('POST', '/definitions/changes', {'id': definition['id'], 'before': 1, 'after': 2})
        self.assertEqual(status, 200)
        self.assertTrue(changes['same_population_definition'])
        self.assertEqual(changes['changes'][0]['field'], 'target_basis_points')
        alternative['target_basis_points'] = 9000
        status, rejected = self.request('POST', '/definitions/register', {'definition': alternative})
        self.assertEqual(status, 409)
        status, listing = self.request('POST', '/definitions/list', {})
        self.assertEqual(status, 200)
        self.assertEqual(len(listing['definitions']), 2)
