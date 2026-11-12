import copy
import json
from pathlib import Path
import unittest
from registry.values import DataError, stamp, iso_day, identifier, integer, day_range, next_day, decimal_ratio
from registry.catalog import Catalog
from bed_events.schema import parse_package, validate_event
from bed_events.replay import snapshot, verify_snapshot, replay_bed
from bed_events.timeline import bed_timeline, exposure
from indicators.definitions import nursing_definition
from audit.revisions import compare_snapshots, revision_impact, explain_bed
from audit.quality import quality_report, revision_inventory


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/registry.json'
EARLY = '2026-11-02T00:00:00Z'
LATEST = '2026-11-10T00:00:00Z'
DAY = '2026-11-01'


def data():
    return json.loads(FIXTURE.read_text())


class EventHistoryTests(unittest.TestCase):
    def setUp(self):
        self.document = data()
        self.catalog, self.events = parse_package(self.document)

    def test_exact_known_time_selects_revision_and_effective_time(self):
        early = snapshot(self.catalog, self.events, DAY, EARLY)
        latest = snapshot(self.catalog, self.events, DAY, LATEST)
        old = next(row for row in early['beds'] if row['bed'] == 'bed-1')
        new = next(row for row in latest['beds'] if row['bed'] == 'bed-1')
        self.assertEqual(old['category'], 'nursing')
        self.assertEqual(new['category'], 'rehabilitation')
        self.assertEqual(len(early['beds']), 5)
        self.assertEqual(len(latest['beds']), 5)
        self.assertTrue(latest['publishable'])
        before = snapshot(self.catalog, self.events, '2026-10-31', LATEST)
        self.assertEqual(next(r for r in before['beds'] if r['bed'] == 'bed-1')['category'], 'general')

    def test_knowledge_timestamp_boundary_is_inclusive(self):
        before = self.events.visible('2026-11-04T23:59:59Z')
        exact = self.events.visible('2026-11-05T00:00:00Z')
        self.assertEqual(next(r for r in before if r['id'] == 'convert-1')['revision'], 1)
        self.assertEqual(next(r for r in exact if r['id'] == 'convert-1')['revision'], 2)
        self.assertEqual(len(exact), len(before))

    def test_end_of_day_stock_includes_last_second(self):
        event = copy.deepcopy(self.document['events'][0])
        event.update(id='new-at-end', bed='at-end', effective_at='2026-11-01T23:59:59Z')
        self.document['events'].append(event)
        catalog, events = parse_package(self.document)
        current = snapshot(catalog, events, DAY, LATEST)
        self.assertTrue(any(row['bed'] == 'at-end' for row in current['beds']))
        event['effective_at'] = '2026-11-02T00:00:00Z'
        catalog, events = parse_package(self.document)
        current = snapshot(catalog, events, DAY, LATEST)
        self.assertFalse(any(row['bed'] == 'at-end' for row in current['beds']))

    def test_simultaneous_competing_events_quarantine_whole_bed(self):
        original = next(row for row in self.document['events'] if row['id'] == 'convert-1')
        self.document['events'].append({**original, 'id': 'competing-conversion', 'category': 'general'})
        catalog, events = parse_package(self.document)
        current = snapshot(catalog, events, DAY, LATEST)
        self.assertFalse(current['publishable'])
        self.assertEqual([row['bed'] for row in current['quarantined']], ['bed-1'])
        self.assertEqual(current['quarantined'][0]['conflicts'][0]['code'], 'simultaneous_bed_events')
        self.assertEqual(len(current['beds']), 4)
        self.assertNotIn('bed-1', [row['bed'] for row in current['beds']])
        self.assertTrue(verify_snapshot(current))

    def test_wrong_prior_category_is_not_silently_repaired(self):
        for row in self.document['events']:
            if row['id'] == 'convert-1':
                row['expected'] = 'nursing'
        catalog, events = parse_package(self.document)
        current = snapshot(catalog, events, DAY, LATEST)
        conflict = current['quarantined'][0]['conflicts'][0]
        self.assertEqual(conflict['code'], 'prior_category_mismatch')
        self.assertEqual(conflict['events'], ['convert-1'])
        self.assertFalse(current['publishable'])

    def test_cancelling_commission_exposes_followup_conflict(self):
        commission = copy.deepcopy(self.document['events'][0])
        commission.update(revision=2, recorded_at='2026-11-06T00:00:00Z', cancelled=True)
        self.document['events'].append(commission)
        catalog, events = parse_package(self.document)
        current = snapshot(catalog, events, DAY, LATEST)
        self.assertEqual(current['quarantined'][0]['conflicts'][0]['code'], 'action_before_commission')
        self.assertEqual(len(events.visible(LATEST, True)), len(events.visible(LATEST)) + 1)
        report = quality_report(catalog, events, DAY, LATEST)
        self.assertEqual(report['cancelled_logical_events'], 1)
        self.assertGreater(report['counts']['error'], 0)

    def test_retirement_and_explicit_reopening(self):
        retired = snapshot(self.catalog, self.events, '2026-11-04', LATEST)
        self.assertEqual([row['bed'] for row in retired['retired']], ['bed-2'])
        event = {'id': 'reopen-2', 'revision': 1, 'recorded_at': '2026-11-05T00:00:00Z', 'bed': 'bed-2', 'facility': 'f1', 'effective_at': '2026-11-05T00:00:00Z', 'action': 'reopen', 'category': 'rehabilitation', 'expected': 'retired', 'cancelled': False}
        self.document['events'].append(event)
        catalog, events = parse_package(self.document)
        current = snapshot(catalog, events, '2026-11-05', LATEST)
        self.assertEqual(next(row for row in current['beds'] if row['bed'] == 'bed-2')['category'], 'rehabilitation')
        self.assertEqual(current['retired'], [])

    def test_revisions_cannot_change_bed_identity_or_skip_versions(self):
        changed = copy.deepcopy(self.document)
        changed['events'][7]['bed'] = 'different'
        with self.assertRaisesRegex(DataError, 'identity'):
            parse_package(changed)
        changed = copy.deepcopy(self.document)
        changed['events'][7]['revision'] = 3
        with self.assertRaisesRegex(DataError, 'contiguous'):
            parse_package(changed)
        changed = copy.deepcopy(self.document)
        changed['events'][7]['recorded_at'] = changed['events'][6]['recorded_at']
        with self.assertRaisesRegex(DataError, 'strictly increase'):
            parse_package(changed)

    def test_registry_correction_changes_ownership_as_known(self):
        correction = {**self.document['facilities'][0], 'revision': 2, 'recorded_at': '2026-11-05T00:00:00Z', 'ownership': 'nonprofit', 'region': 'village'}
        self.document['facilities'].append(correction)
        catalog, events = parse_package(self.document)
        before = snapshot(catalog, events, DAY, EARLY)
        after = snapshot(catalog, events, DAY, LATEST)
        old = next(row for row in before['beds'] if row['bed'] == 'bed-3')
        new = next(row for row in after['beds'] if row['bed'] == 'bed-3')
        self.assertEqual((old['region'], old['ownership']), ('town', 'public'))
        self.assertEqual((new['region'], new['ownership']), ('village', 'nonprofit'))
        self.assertEqual(new['facility_revision'], 2)

    def test_facility_close_is_exclusive_and_license_violation_observable(self):
        self.document['facilities'][0]['close'] = DAY
        catalog, events = parse_package(self.document)
        current = snapshot(catalog, events, DAY, LATEST)
        self.assertEqual(len(current['excluded']), 3)
        self.assertEqual(len(current['beds']), 2)
        self.assertTrue(all(row['reason'] == 'facility_not_effective_or_not_known' for row in current['excluded']))
        self.document = data()
        self.document['facilities'][0]['license_limit'] = 2
        catalog, events = parse_package(self.document)
        current = snapshot(catalog, events, DAY, LATEST)
        self.assertFalse(current['publishable'])
        self.assertEqual(current['license_violations'][0]['active_beds'], 3)

    def test_snapshot_checksum_and_disjoint_partitions(self):
        current = snapshot(self.catalog, self.events, DAY, LATEST)
        self.assertTrue(verify_snapshot(current))
        current['beds'][0]['category'] = 'general'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            verify_snapshot(current)
        current = snapshot(self.catalog, self.events, DAY, LATEST)
        current['retired'].append(copy.deepcopy(current['beds'][0]))
        from registry.values import fingerprint
        current['snapshot_digest'] = fingerprint({k: v for k, v in current.items() if k != 'snapshot_digest'})
        with self.assertRaisesRegex(ValueError, 'multiple'):
            verify_snapshot(current)

    def test_continuous_exposure_and_daily_stock_are_distinct(self):
        timeline = bed_timeline(self.catalog, self.events, 'bed-1', '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z', EARLY)
        self.assertEqual(sum(row['seconds'] for row in timeline['intervals']), 86400)
        nursing_seconds = sum(row['seconds'] for row in timeline['intervals'] if row['category'] == 'nursing')
        self.assertEqual(nursing_seconds, 16 * 3600)
        current = snapshot(self.catalog, self.events, DAY, EARLY)
        self.assertEqual(next(r for r in current['beds'] if r['bed'] == 'bed-1')['category'], 'nursing')
        observed = exposure(self.catalog, self.events, '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z', EARLY, nursing_definition())
        county = next(r for r in observed['regions'] if r['region'] == 'county')
        self.assertEqual(county['denominator_seconds'], 5 * 86400)
        self.assertEqual(county['numerator_seconds'], 2 * 86400 + 16 * 3600)

    def test_revision_impact_explains_only_changed_bed_day(self):
        impact = revision_impact(self.catalog, self.events, [DAY], EARLY, LATEST, nursing_definition())
        self.assertEqual(impact['changed_bed_day_cells'], 1)
        self.assertEqual(impact['days'][0]['beds']['changes'][0]['bed'], 'bed-1')
        explained = explain_bed(self.catalog, self.events, 'bed-1', DAY, EARLY)
        self.assertEqual(explained['future_knowledge_revisions'], 1)
        self.assertEqual(explained['partition'], 'beds')
        self.assertEqual(len(explained['replay_trace']), 2)
        inventory = revision_inventory(self.catalog, self.events)
        self.assertEqual(next(row for row in inventory['event_revisions'] if row['event'] == 'convert-1')['revisions'], 2)

    def test_input_calendar_numeric_and_hierarchy_boundaries(self):
        for value in ('2026-11-01T00:00:00+00:00', '2026-11-01', '2026-02-30T00:00:00Z'):
            with self.subTest(value=value), self.assertRaises(DataError):
                stamp(value, 'timestamp')
        self.assertEqual(next_day('2024-02-28'), '2024-02-29')
        self.assertEqual(day_range('2024-02-28', '2024-03-01'), ['2024-02-28', '2024-02-29'])
        for value in (True, -1, 1.2):
            with self.subTest(value=value), self.assertRaises(DataError):
                integer(value, 'count')
        self.document['regions'][0]['parent'] = 'town'
        with self.assertRaisesRegex(DataError, 'cycle'):
            parse_package(self.document)
        self.assertIsNone(decimal_ratio(0, 0))
        self.assertEqual(decimal_ratio(1, 2), '0.500000000000')
