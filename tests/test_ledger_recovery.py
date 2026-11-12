import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from test_event_history import data, DAY, EARLY, LATEST
from registry.values import fingerprint, canonical
from registry.intake import preview, extract_delta
from bed_events.ledger import Ledger, RevisionConflict, IntegrityFailure
from bed_events.schema import parse_package
from bed_events.replay import snapshot
from bed_events.staging import Staging
from audit.jobs import SnapshotQueue
from audit.review import Reviews
from audit.triage import Triage, issues
from audit.provenance import audit_provenance, lineage_for_bed
from audit.recovery import backup, restore
from indicators.registry import Definitions
from indicators.definitions import nursing_definition


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / 'ledger.sqlite'
        self.ledger = Ledger(self.path)
        self.document = data()

    def tearDown(self):
        self.ledger.close()
        self.temporary.cleanup()

    def ingest(self):
        return self.ledger.ingest('first', self.document)

    def test_atomic_import_and_duplicate_request(self):
        first = self.ingest()
        second = self.ledger.ingest('first', self.document)
        self.assertEqual(first, second)
        self.assertEqual(first['summary']['events']['added'], 9)
        self.assertEqual(len(self.ledger.history()), 1)
        self.assertEqual(len(self.ledger.document()['events']), 9)
        different = copy.deepcopy(self.document)
        different['events'].pop()
        with self.assertRaises(RevisionConflict):
            self.ledger.ingest('first', different)
        self.assertEqual(len(self.ledger.history()), 1)

    def test_failed_import_leaves_no_partial_rows_or_chain_entry(self):
        bad = copy.deepcopy(self.document)
        bad['events'][0]['facility'] = 'unknown'
        with self.assertRaises(ValueError):
            self.ledger.ingest('bad', bad)
        self.assertEqual(self.ledger.history(), [])
        self.assertEqual(self.ledger.document()['regions'], [])
        self.assertEqual(self.ledger.document()['facilities'], [])
        self.assertEqual(self.ledger.document()['events'], [])
        self.assertTrue(self.ledger.audit()['valid'])

    def test_existing_revision_is_immutable_even_with_new_key(self):
        self.ingest()
        changed = copy.deepcopy(self.document)
        changed['events'][0]['category'] = 'nursing'
        with self.assertRaises(RevisionConflict):
            self.ledger.ingest('different-key', changed)
        self.assertEqual(self.ledger.document()['events'][0]['category'], 'general')
        unchanged = self.ledger.ingest('same-data-new-key', self.document)
        self.assertEqual(unchanged['summary']['events']['added'], 0)
        self.assertEqual(unchanged['summary']['events']['unchanged'], 9)
        self.assertTrue(audit_provenance(self.ledger)['valid'])

    def test_correction_append_and_publication_preserve_old_snapshot(self):
        base = copy.deepcopy(self.document)
        base['events'] = [row for row in base['events'] if not (row['id'] == 'convert-1' and row['revision'] == 2)]
        self.ledger.ingest('before-correction', base)
        prior = self.ledger.create_snapshot('old', DAY, EARLY)
        self.ledger.publish('public-old', 'old')
        delta = extract_delta(base, self.document)
        self.assertEqual(len(delta['events']), 1)
        receipt = self.ledger.ingest('correction', delta)
        self.assertEqual(receipt['summary']['events']['added'], 1)
        current = self.ledger.create_snapshot('new', DAY, LATEST)
        self.assertNotEqual(prior['result_digest'], current['result_digest'])
        self.assertEqual(self.ledger.publication('public-old')['snapshot_data']['result'], prior['result'])
        with self.assertRaises(RevisionConflict):
            self.ledger.create_snapshot('old', DAY, EARLY)
        self.assertEqual(self.ledger.audit()['snapshots'], 2)

    def test_snapshot_and_publication_identity_cannot_be_rebound(self):
        self.ingest()
        first = self.ledger.create_snapshot('current', DAY, LATEST)
        self.assertEqual(first, self.ledger.create_snapshot('current', DAY, LATEST))
        self.ledger.publish('public', 'current')
        self.ledger.create_snapshot('early', DAY, EARLY)
        with self.assertRaises(RevisionConflict):
            self.ledger.publish('public', 'early')
        with self.assertRaises(RevisionConflict):
            self.ledger.create_snapshot('current', '2026-11-02', LATEST)
        self.assertEqual(self.ledger.publication('public')['snapshot'], 'current')

    def test_conflicted_snapshot_cannot_publish_or_approve(self):
        duplicate = {**self.document['events'][6], 'id': 'competing'}
        self.document['events'].append(duplicate)
        self.ingest()
        current = self.ledger.create_snapshot('conflict', DAY, LATEST)
        self.assertFalse(current['result']['publishable'])
        with self.assertRaises(RevisionConflict):
            self.ledger.publish('invalid', 'conflict')
        reviews = Reviews(self.ledger)
        reviews.open('check', 'conflict', nursing_definition().export())
        with self.assertRaises(RevisionConflict):
            reviews.decide('check', 1, 'analyst', True, 'cannot approve conflict')
        self.assertEqual(reviews.get('check')['state'], 'open')

    def test_hash_chain_and_request_provenance_detect_tampering(self):
        self.ingest()
        provenance = audit_provenance(self.ledger)
        self.assertTrue(provenance['valid'])
        self.assertEqual(provenance['anchored_rows'], 14)
        lineage = lineage_for_bed(self.ledger, 'bed-1')
        self.assertEqual(len(lineage['events']), 3)
        self.assertTrue(all(row['batch_sequence'] == 1 for row in lineage['events']))
        self.ledger.db.execute("UPDATE batch_requests SET body='{}' WHERE sequence=1")
        with self.assertRaisesRegex(IntegrityFailure, 'request evidence'):
            self.ledger.audit()

    def test_chain_receipt_and_body_checks_are_independent(self):
        self.ingest()
        self.ledger.db.execute("UPDATE batches SET previous_hash='bad' WHERE sequence=1")
        with self.assertRaisesRegex(IntegrityFailure, 'checksum'):
            self.ledger.batch(1)
        self.ledger.db.execute("UPDATE events SET body='{}' WHERE id='commission-1'")
        with self.assertRaisesRegex(IntegrityFailure, 'body checksum'):
            self.ledger.document()

    def test_racing_identical_imports_create_one_batch(self):
        def ingest(_):
            with Ledger(self.path) as ledger:
                return ledger.ingest('race', self.document)['chain_hash']
        with ThreadPoolExecutor(max_workers=3) as pool:
            hashes = list(pool.map(ingest, range(6)))
        self.assertEqual(len(set(hashes)), 1)
        self.assertEqual(len(self.ledger.history()), 1)
        self.assertEqual(self.ledger.audit()['batches'], 1)

    def test_staging_can_reconcile_out_of_order_revisions(self):
        staging = Staging(self.ledger)
        first = copy.deepcopy(self.document)
        correction = next(row for row in first['events'] if row['id'] == 'convert-1' and row['revision'] == 2)
        first['events'].remove(correction)
        second = {'version': 1, 'regions': [], 'facilities': [], 'events': [correction]}
        staging.put('late-revision', second)
        with self.assertRaises(ValueError):
            staging.inspect({'late-revision': 1})
        staging.put('base-source', first)
        selections = {'late-revision': 1, 'base-source': 1}
        checked = staging.inspect(selections)
        result = staging.promote(selections, 'staged-import', checked['combined_digest'])
        self.assertEqual(result['batch']['summary']['events']['added'], 9)
        self.assertTrue(all(row['state'] == 'promoted' for row in result['stages']))
        self.assertEqual(staging.pending(), [])
        self.assertEqual(staging.audit()['promoted'], 2)
        self.assertTrue(self.ledger.audit()['valid'])

    def test_staging_revision_conflicts_and_atomic_failure(self):
        staging = Staging(self.ledger)
        stage = staging.put('one', self.document)
        self.assertEqual(stage['revision'], 1)
        with self.assertRaises(RevisionConflict):
            staging.put('one', self.document, 9)
        reviewed = staging.inspect({'one': 1})
        staging.put('one', self.document, 1)
        with self.assertRaises(RevisionConflict):
            staging.promote({'one': 1}, 'stale', reviewed['combined_digest'])
        self.assertEqual(self.ledger.history(), [])
        with self.assertRaises(RevisionConflict):
            staging.promote({'one': 2}, 'digest-conflict', 'wrong')
        self.assertEqual(staging.get('one')['state'], 'draft')
        self.assertIn('changed', staging.get('one')['error'])

    def test_job_expiry_fences_stale_worker_and_preserves_input(self):
        self.ingest()
        queue = SnapshotQueue(self.ledger)
        job = queue.enqueue('snapshot-job', DAY, LATEST)
        old = queue.claim('snapshot-job', 'old', 10, 5)
        with self.assertRaises(RevisionConflict):
            queue.claim('snapshot-job', 'new', 14)
        new = queue.claim('snapshot-job', 'new', 15)
        catalog, events = parse_package(job['document'])
        result = snapshot(catalog, events, DAY, LATEST)
        with self.assertRaises(RevisionConflict):
            queue.complete(old, result, 16)
        done = queue.complete(new, result, 16)
        self.assertEqual(done['state'], 'done')
        self.assertEqual(done['result'], result)
        self.assertEqual(done['generation'], 2)
        self.assertTrue(self.ledger.audit()['valid'])

    def test_job_failure_retry_is_explicit(self):
        self.ingest()
        queue = SnapshotQueue(self.ledger)
        queue.enqueue('failed-job', DAY, LATEST)
        token = queue.claim('failed-job', 'worker', 1, 100)
        queue.fail(token, 'simulated worker failure', 2)
        self.assertEqual(queue.get('failed-job')['state'], 'failed')
        with self.assertRaises(RevisionConflict):
            queue.claim('failed-job', 'worker', 3)
        queue.retry('failed-job')
        outcome = queue.work('failed-job', clock=lambda: 3)
        self.assertEqual(outcome['state'], 'done')
        self.assertIsNone(outcome['error'])

    def test_review_decisions_are_versioned_and_immutable(self):
        self.ingest()
        self.ledger.create_snapshot('current', DAY, LATEST)
        reviews = Reviews(self.ledger)
        reviews.open('review', 'current', nursing_definition().export())
        with self.assertRaises(RevisionConflict):
            reviews.approved_public_report('review')
        approved = reviews.decide('review', 1, 'analyst', True, 'reviewed synthetic evidence')
        self.assertEqual(approved['revision'], 2)
        self.assertEqual(approved['state'], 'approved')
        public = reviews.approved_public_report('review')
        self.assertEqual(public['reviewer'], 'analyst')
        with self.assertRaises(RevisionConflict):
            reviews.decide('review', 1, 'other', False, 'stale')
        self.assertEqual(reviews.audit()['reviews'], 1)

    def test_definition_versions_cannot_change_population_silently(self):
        definitions = Definitions(self.ledger)
        original = nursing_definition().export()
        first = definitions.register(original)
        self.assertEqual(definitions.register(original), first)
        alternative = {**original, 'ownerships': ['public']}
        with self.assertRaises(RevisionConflict):
            definitions.register(alternative)
        alternative['version'] = 2
        definitions.register(alternative)
        self.assertFalse(definitions.changes(original['id'], 1, 2)['same_population_definition'])
        self.assertEqual(definitions.audit()['definitions'], 2)
        self.assertTrue(self.ledger.audit()['valid'])

    def test_backup_restore_replays_all_saved_evidence(self):
        self.ingest()
        self.ledger.create_snapshot('current', DAY, LATEST)
        self.ledger.publish('public', 'current')
        saved = backup(self.ledger, self.root / 'backup.sqlite')
        outcome = restore(saved['path'], self.root / 'restored.sqlite', saved['digest'])
        self.assertTrue(outcome['audit']['valid'])
        with Ledger(self.root / 'restored.sqlite') as restored:
            self.assertEqual(restored.publication('public')['snapshot_data']['result'], self.ledger.get_snapshot('current')['result'])
        with self.assertRaises(ValueError):
            restore(saved['path'], self.root / 'wrong.sqlite', 'incorrect')
        with self.assertRaises(FileExistsError):
            backup(self.ledger, self.root / 'backup.sqlite')
        self.assertFalse((self.root / 'wrong.sqlite').exists())

    def test_preview_validates_contract_and_does_not_write(self):
        checked = preview(self.ledger, self.document)
        self.assertEqual(checked['counts']['events']['new'], 9)
        self.assertEqual(checked['changed_beds'], ['bed-1', 'bed-2', 'bed-3', 'bed-4', 'bed-5', 'bed-6'])
        self.assertEqual(self.ledger.history(), [])
        self.assertEqual(self.ledger.document()['events'], [])
        wrong = {**self.document, 'version': 2}
        with self.assertRaises(ValueError):
            preview(self.ledger, wrong)
        wrong = {**self.document, 'unknown': []}
        with self.assertRaises(ValueError):
            preview(self.ledger, wrong)
        self.assertEqual(self.ledger.history(), [])

    def test_audit_detects_incomplete_working_job_lease(self):
        self.ingest()
        queue = SnapshotQueue(self.ledger)
        queue.enqueue('job', DAY, LATEST)
        queue.claim('job', 'worker', 10, 20)
        self.ledger.db.execute("UPDATE snapshot_jobs SET owner=NULL WHERE id='job'")
        with self.assertRaisesRegex(ValueError, 'incomplete lease'):
            self.ledger.audit()
        self.ledger.db.execute("UPDATE snapshot_jobs SET owner='worker' WHERE id='job'")
        self.assertTrue(self.ledger.audit()['valid'])
