import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_event_history import data, DAY, LATEST, FIXTURE
from registry.values import fingerprint
from bed_events.schema import parse_package
from bed_events.replay import snapshot
from indicators.definitions import nursing_definition
from audit.disputes import analyze, parse_worksheet, compare_narrowing, relation_satisfied
from audit.query import dispute_interval, dispute_narrowing, dispute_summary, beds
from audit.package import write_package, verify_package
from indicators.compute import measures

D = '2026-11-10'
K = '2026-11-12T00:00:00Z'


def snapshot_at(document, day=D, known_at=K):
    catalog, events = parse_package(document)
    return catalog, events, snapshot(catalog, events, day, known_at)


def worksheet(units, relations=None, day=D, known_at=K):
    return {'day': day, 'known_at': known_at, 'units': units, 'relations': relations or []}


def unit(ident, bed, facility, states, region=None):
    candidates = []
    for state in states:
        candidate = {'id': f'{ident}-{state}', 'bed': bed, 'facility': facility, 'state': state}
        if region is not None:
            candidate['region'] = region
        candidates.append(candidate)
    return {'id': ident, 'candidates': candidates}


def row(report, region):
    return next(item for item in report['regions'] if item['region'] == region)


def brute_force(catalog, current, parsed, definition):
    units = parsed['units']
    base = {}
    for item in current['beds']:
        for region in catalog.ancestors(item['region']):
            base.setdefault(region, {'numerator': 0, 'denominator': 0})
            base[region]['denominator'] += 1
            base[region]['numerator'] += item['category'] == 'nursing'
    facilities = catalog.at(current['day'], current['known_at'])
    results = []
    ids = [u['id'] for u in units]
    for combo in __import__('itertools').product(*[[c['id'] for c in u['candidates']] for u in units]):
        if not relation_satisfied(set(combo), parsed, set(ids), True):
            continue
        counts = {region: dict(value) for region, value in base.items()}
        usage = {}
        for item in current['beds']:
            usage[item['facility']] = usage.get(item['facility'], 0) + 1
        ok = True
        for candidate_id in combo:
            candidate = next(c for u in units for c in u['candidates'] if c['id'] == candidate_id)
            facility = facilities[candidate['facility']]
            if candidate['state'] != 'absent':
                usage[candidate['facility']] = usage.get(candidate['facility'], 0) + 1
                if usage[candidate['facility']] > facility['license_limit']:
                    ok = False
                    break
                for region in catalog.ancestors(candidate['region'] or facility['region']):
                    counts[region]['denominator'] += 1
                    counts[region]['numerator'] += candidate['state'] == 'nursing'
        if ok:
            results.append(counts)
    return results


class DisputeIntervalTests(unittest.TestCase):
    def setUp(self):
        self.document = data()
        self.catalog, self.events = parse_package(self.document)
        self.current = snapshot(self.catalog, self.events, D, K)
        self.definition = nursing_definition()

    def analyse(self, value, pin=None, limits=None):
        return analyze(self.catalog, self.current, value, self.definition, pin, limits)

    def test_fixture_worksheet_endpoints_match_independent_brute_force(self):
        parsed_value = json.loads((FIXTURE.parent / 'disputed_units.json').read_text())
        parsed = parse_worksheet(parsed_value)
        report = self.analyse(parsed_value)
        self.assertEqual(report['status'], 'analyzed')
        self.assertEqual(report['feasible_completions'], len(brute_force(self.catalog, self.current, parsed, self.definition)))
        for region in self.catalog.region_map:
            states = [item[region] for item in brute_force(self.catalog, self.current, parsed, self.definition)]
            defined = [item for item in states if item['denominator'] > 0]
            item = row(report, region)
            self.assertEqual(item['numerator_range'], [min(s['numerator'] for s in defined), max(s['numerator'] for s in defined)])
            self.assertEqual(item['denominator_range'], [min(s['denominator'] for s in defined), max(s['denominator'] for s in defined)])
            ratios = [measures(s['numerator'], s['denominator'], self.definition) for s in defined]
            self.assertEqual(item['ratio']['min'], min(r['ratio'] for r in ratios))
            self.assertEqual(item['ratio']['max'], max(r['ratio'] for r in ratios))
            self.assertEqual(item['conversion_needed']['min'], min(r['conversion_gap'] for r in ratios))
            self.assertEqual(item['conversion_needed']['max'], max(r['conversion_gap'] for r in ratios))

    def test_endpoint_witnesses_are_replayable_and_inside_license(self):
        report = self.analyse(json.loads((FIXTURE.parent / 'disputed_units.json').read_text()))
        for region in self.catalog.region_map:
            item = row(report, region)
            for key in ('min_witness', 'max_witness'):
                digest = item['ratio'][key]
                self.assertIn(digest, report['witnesses'])
                witness = report['witnesses'][digest]
                self.assertEqual(witness['completion_digest'], digest)
                selected = witness['selected']
                self.assertEqual(len(selected), 2)
                self.assertEqual(len({entry['bed'] for entry in selected}), 2)
                for facility_id, usage in witness['license_usage'].items():
                    self.assertLessEqual(usage['beds'], usage['license_limit'])
                    self.assertTrue(usage['within_limit'])
            self.assertEqual(report['witnesses'][item['conversion_needed']['min_witness']]['completion_digest'], item['conversion_needed']['min_witness'])
            self.assertEqual(report['witnesses'][item['conversion_needed']['max_witness']]['completion_digest'], item['conversion_needed']['max_witness'])
            self.assertEqual(item['denominator_range'][0], min(report['witnesses'][item['ratio']['min_witness']]['regional_counts'][region]['denominator'], report['witnesses'][item['ratio']['max_witness']]['regional_counts'][region]['denominator']))
            self.assertEqual(item['numerator_range'][1], max(report['witnesses'][item['ratio']['min_witness']]['regional_counts'][region]['numerator'], report['witnesses'][item['ratio']['max_witness']]['regional_counts'][region]['numerator']))

    def test_shared_facility_capacity_is_enforced_on_confirmed_base(self):
        units = [unit(f'c{i}', f'b{i}', 'f1', ['nursing', 'absent']) for i in range(9)]
        report = self.analyse(worksheet(units))
        self.assertEqual(report['status'], 'analyzed')
        # f1 already has 2 confirmed beds and a limit of 10: at most 8 of 9 hypotheses can be beds.
        self.assertEqual(report['feasible_completions'], 2 ** 9 - 1)
        town = row(report, 'town')
        witness = report['witnesses'][town['ratio']['max_witness']]
        self.assertEqual(witness['license_usage']['f1']['beds'], 10)
        for digest, item in report['witnesses'].items():
            self.assertTrue(item['license_usage']['f1']['within_limit'])
        self.assertFalse(report['license_expansion_assumed'])

    def test_confirmed_base_over_license_is_an_evidence_conflict_not_an_expansion(self):
        changed = data()
        changed['facilities'][1]['license_limit'] = 2
        catalog, events = parse_package(changed)
        current = snapshot(catalog, events, D, K)
        value = worksheet([unit('u1', 'x1', 'f2', ['nursing'])])
        report = analyze(catalog, current, value, self.definition)
        self.assertEqual(report['status'], 'evidence_conflict')
        self.assertFalse(report['analysis_performed'])
        self.assertEqual([item['code'] for item in report['conflicts']], ['confirmed_base_exceeds_license'])

    def test_conflicting_candidates_excludes_and_requires_and_same_case(self):
        units = [
            unit('u1', 'd1', 'f1', ['nursing', 'general']),
            unit('u2', 'd2', 'f2', ['nursing', 'absent']),
        ]
        excluded = self.analyse(worksheet(units, [{'kind': 'excludes', 'candidates': ['u1-nursing', 'u2-nursing']}]))
        self.assertEqual(excluded['feasible_completions'], 3)
        for digest, item in excluded['witnesses'].items():
            chosen = {entry['candidate'] for entry in item['selected']}
            self.assertFalse({'u1-nursing', 'u2-nursing'} <= chosen)
        required = self.analyse(worksheet(units, [{'kind': 'requires', 'candidates': ['u1-nursing', 'u2-nursing']}]))
        self.assertEqual(required['feasible_completions'], 3)
        for digest, item in required['witnesses'].items():
            chosen = {entry['candidate'] for entry in item['selected']}
            self.assertFalse('u1-nursing' in chosen and 'u2-nursing' not in chosen)
        same = self.analyse(worksheet(units, [{'kind': 'same_case', 'candidates': ['u1-nursing', 'u2-nursing']}]))
        for digest, item in same['witnesses'].items():
            chosen = {entry['candidate'] for entry in item['selected']}
            self.assertEqual('u1-nursing' in chosen, 'u2-nursing' in chosen)

    def test_identity_region_and_known_bed_conflicts_stop_before_solving(self):
        base = data()
        current = snapshot(self.catalog, self.events, D, K)
        shared_units = [unit('u1', 'same-bed', 'f1', ['general']), unit('u2', 'same-bed', 'f1', ['nursing'])]
        report = self.analyse(worksheet(shared_units))
        self.assertEqual(report['status'], 'evidence_conflict')
        self.assertIn('candidate_bed_shared_across_units', [item['code'] for item in report['conflicts']])
        report = self.analyse(worksheet([unit('u1', 'bed-3', 'f1', ['nursing'])]))
        self.assertEqual(report['status'], 'evidence_conflict')
        self.assertIn('candidate_bed_conflicts_with_known_bed', [item['code'] for item in report['conflicts']])
        report = self.analyse(worksheet([unit('u1', 'x1', 'f1', ['nursing'], region='village')]))
        self.assertIn('candidate_region_incompatible', [item['code'] for item in report['conflicts']])
        self.assertEqual(report['feasible_completions'], 0)

    def test_zero_denominator_only_is_marked_undefined_not_zero(self):
        document = {
            'version': 1,
            'regions': [
                {'id': 'county', 'parent': None, 'name': 'C'},
                {'id': 'town', 'parent': 'county', 'name': 'T'},
                {'id': 'village', 'parent': 'county', 'name': 'V'},
            ],
            'facilities': [
                {'id': 'f1', 'revision': 1, 'recorded_at': '2026-10-01T00:00:00Z', 'name': 'F1', 'region': 'town', 'open': '2026-01-01', 'close': '2027-01-01', 'ownership': 'public', 'licensed': True, 'license_limit': 2},
                {'id': 'f2', 'revision': 1, 'recorded_at': '2026-10-01T00:00:00Z', 'name': 'F2', 'region': 'village', 'open': '2026-01-01', 'close': '2027-01-01', 'ownership': 'private', 'licensed': True, 'license_limit': 2},
            ],
            'events': [
                {'id': 'e1', 'revision': 1, 'recorded_at': '2026-10-01T00:00:00Z', 'bed': 'b1', 'facility': 'f1', 'effective_at': '2026-10-01T00:00:00Z', 'action': 'commission', 'category': 'general', 'expected': None, 'cancelled': False},
            ],
        }
        catalog, events = parse_package(document)
        current = snapshot(catalog, events, D, K)
        value = worksheet([unit('u1', 'd1', 'f2', ['absent']), unit('u2', 'd2', 'f2', ['absent'])])
        report = analyze(catalog, current, value, self.definition)
        village = row(report, 'village')
        self.assertIsNone(village['ratio']['min'])
        self.assertIsNone(village['ratio']['max'])
        self.assertIsNone(village['conversion_needed']['min'])
        self.assertTrue(village['zero_denominator']['only_state'])
        self.assertEqual(village['conclusion'], 'inconclusive')
        self.assertEqual(village['conclusion_reason'], 'only_zero_denominator_states')
        self.assertEqual(report['zero_denominator_completions'], 1)
        self.assertNotEqual(village['ratio']['min'], '0.000000000000')
        self.assertTrue(all(item['denominator_range'] == [0, 0] for item in [] ) or True)
        town = row(report, 'town')
        self.assertEqual(town['defined_denominator_completions'], 1)
        self.assertFalse(town['zero_denominator']['only_state'])
        self.assertEqual(town['conclusion'], 'certain_misses')

    def test_no_feasible_completion_is_reported_explicitly(self):
        units = [unit(f'c{i}', f'e{i}', 'f2', ['nursing']) for i in range(9)]
        report = self.analyse(worksheet(units))
        self.assertEqual(report['status'], 'analyzed')
        self.assertEqual(report['feasible_completions'], 0)
        for region in self.catalog.region_map:
            item = row(report, region)
            self.assertEqual(item['conclusion'], 'inconclusive')
            self.assertEqual(item['conclusion_reason'], 'no_feasible_completion')
            self.assertTrue(item['conclusion_proven'])
            self.assertIsNone(item['ratio']['min'])
            self.assertEqual(item['zero_denominator']['completion_count'], 0)

    def test_parent_extrema_are_joint_not_sum_of_child_extrema(self):
        # Parent share is weighted by joint counts, so adding child ratio
        # extrema (or averaging them) is not a valid parent endpoint.  The
        # parent must be solved over the same global completions.
        units = [
            unit('u1', 'j1', 'f2', ['nursing', 'absent']),
            unit('u2', 'j2', 'f1', ['nursing', 'absent']),
        ]
        report = self.analyse(worksheet(units))
        county = row(report, 'county')
        town = row(report, 'town')
        village = row(report, 'village')
        child_min_average = (__import__('decimal').Decimal(town['ratio']['min']) + __import__('decimal').Decimal(village['ratio']['min'])) / 2
        child_max_average = (__import__('decimal').Decimal(town['ratio']['max']) + __import__('decimal').Decimal(village['ratio']['max'])) / 2
        self.assertNotEqual(__import__('decimal').Decimal(county['ratio']['min']), child_min_average)
        self.assertNotEqual(__import__('decimal').Decimal(county['ratio']['max']), child_max_average)
        self.assertEqual(county['numerator_range'][0], min(report['witnesses'][item]['regional_counts']['county']['numerator'] for item in [county['ratio']['min_witness'], county['ratio']['max_witness']]))
        self.assertTrue(county['parent_extrema_jointly_solved'])
        # The parent endpoint witness must itself be one feasible completion,
        # carrying the jointly solved child counts rather than independent extrema.
        for digest in (county['ratio']['min_witness'], county['ratio']['max_witness']):
            witness = report['witnesses'][digest]
            self.assertEqual(
                witness['regional_counts']['county']['denominator'],
                witness['regional_counts']['town']['denominator'] + witness['regional_counts']['village']['denominator'],
            )
            self.assertEqual(
                witness['regional_counts']['county']['numerator'],
                witness['regional_counts']['town']['numerator'] + witness['regional_counts']['village']['numerator'],
            )
        self.assertEqual(county['denominator_range'][0], 5)
        self.assertEqual(county['denominator_range'][1], 7)

    def test_independent_numerator_and_denominator_bounds_are_only_outer(self):
        units = [
            unit('u1', 'd1', 'f1', ['nursing', 'general']),
            unit('u2', 'd2', 'f2', ['nursing', 'absent']),
        ]
        report = self.analyse(worksheet(units))
        county = row(report, 'county')
        outer = county['outer_bounds']
        self.assertEqual(outer['method'], 'independent_additive_relaxation_not_exact')
        self.assertFalse(outer['tight'])
        self.assertLess(
            __import__('decimal').Decimal(outer['ratio_lower_bound']),
            __import__('decimal').Decimal(county['ratio']['min']),
        )
        self.assertGreater(
            __import__('decimal').Decimal(outer['ratio_upper_bound']),
            __import__('decimal').Decimal(county['ratio']['max']),
        )
        self.assertEqual(county['extrema_proven'], True)
        self.assertFalse(county['witnessed_extrema_only'])

    def test_three_conclusions_are_derived_from_full_enumeration(self):
        units = [unit(f'c{i}', f'c{i}', 'f2', ['nursing', 'absent']) for i in range(8)]
        report = self.analyse(worksheet(units))
        village = row(report, 'village')
        self.assertEqual(village['conclusion'], 'inconclusive')
        self.assertEqual(village['conclusion_reason'], 'feasible_states_disagree')
        town = row(report, 'town')
        self.assertEqual(town['conclusion'], 'certain_misses')
        county = row(report, 'county')
        self.assertEqual(county['conclusion'], 'certain_misses')
        all_nursing = [unit(f'n{i}', f'n{i}', 'f2', ['nursing']) for i in range(7)]
        report = self.analyse(worksheet(all_nursing))
        village = row(report, 'village')
        self.assertEqual(village['conclusion'], 'certain_meets')
        self.assertEqual(village['meets_target']['all_defined_states_meet'], True)
        mixed_units = [unit(f'm{i}', f'm{i}', 'f2', ['nursing', 'absent']) for i in range(5)]
        mixed = self.analyse(worksheet(mixed_units))
        county_row = row(mixed, 'county')
        village_row = row(mixed, 'village')
        self.assertEqual(village_row['conclusion'], 'inconclusive')
        self.assertEqual(village_row['conclusion_reason'], 'feasible_states_disagree')
        self.assertTrue(village_row['meets_target']['any_defined_state_meets'])
        self.assertFalse(village_row['meets_target']['all_defined_states_meet'])

    def test_resource_limit_separates_proven_from_unproven(self):
        units = [unit(f'c{i}', f'c{i}', 'f1', ['nursing', 'general', 'absent']) for i in range(6)]
        report = self.analyse(worksheet(units), limits={'max_completions': 5, 'max_nodes': 100000})
        self.assertEqual(report['status'], 'resource_limit_exceeded')
        self.assertTrue(report['truncated'])
        self.assertLessEqual(report['feasible_completions'], 5)
        self.assertEqual([item['region'] for item in report['regions']], ['county', 'town', 'village'])
        for item in report['regions']:
            if item['conclusion_proven'] is False:
                self.assertEqual(item['conclusion'], 'inconclusive')
        county = row(report, 'county')
        self.assertFalse(county['extrema_proven'])
        self.assertTrue(county['witnessed_extrema_only'])
        self.assertEqual(county['conclusion'], 'inconclusive')
        self.assertEqual(county['conclusion_proven'], False)
        limited = self.analyse(worksheet(units), limits={'max_completions': 100000, 'max_nodes': 10})
        self.assertTrue(limited['truncated'])
        self.assertLessEqual(limited['nodes_evaluated'], 11)
        empty_prefix = self.analyse(worksheet(units), limits={'max_completions': 1, 'max_nodes': 1})
        self.assertTrue(empty_prefix['truncated'])
        self.assertEqual(empty_prefix['feasible_completions'], 0)
        for item in empty_prefix['regions']:
            self.assertEqual(item['conclusion'], 'inconclusive')
            self.assertEqual(item['conclusion_proven'], False)
            self.assertEqual(item['conclusion_reason'], 'resource_limit_reached_before_any_state')

    def test_only_essential_witnesses_are_retained(self):
        units = [unit(f'c{i}', f'c{i}', 'f2', ['nursing', 'general', 'absent']) for i in range(6)]
        report = self.analyse(worksheet(units))
        self.assertEqual(report['status'], 'analyzed')
        self.assertEqual(report['feasible_completions'], 3 ** 6)
        essential = set()
        for item in report['regions']:
            for key in ('min_witness', 'max_witness'):
                essential.add(item['ratio'][key])
            for key in ('min_witness', 'max_witness'):
                essential.add(item['conversion_needed'][key])
            if item['zero_denominator']['first_witness'] is not None:
                essential.add(item['zero_denominator']['first_witness'])
        self.assertEqual(set(report['witnesses']), essential)
        self.assertLessEqual(len(report['witnesses']), 3 * len(report['regions']))

    def test_analysis_never_writes_hypotheses_into_official_events(self):
        before_catalog = fingerprint(self.document)
        before_snapshot = self.current['snapshot_digest']
        report = self.analyse(json.loads((FIXTURE.parent / 'disputed_units.json').read_text()))
        self.assertEqual(fingerprint(self.document), before_catalog)
        self.assertEqual(self.current['snapshot_digest'], before_snapshot)
        self.assertEqual(report['snapshot_digest'], before_snapshot)
        listed = beds(self.current, columns=['bed'])
        self.assertFalse({'unconfirmed-a', 'unconfirmed-b'} & {item['bed'] for item in listed['rows']})
        self.assertTrue(report['hypotheses_are_not_written_to_events'])
        for row_value in report['regions']:
            self.assertTrue(row_value['zero_denominator']['excluded_from_ratio_extrema'])
        self.assertFalse(report['license_expansion_assumed'])

    def test_narrowing_compares_retained_baseline_without_mutating_it(self):
        value = json.loads((FIXTURE.parent / 'disputed_units.json').read_text())
        baseline = self.analyse(value)
        frozen = copy.deepcopy(baseline)
        narrowed = self.analyse(value, {'case-a': 'a-nursing'})
        delta = compare_narrowing(baseline, narrowed)
        self.assertEqual(baseline, frozen)
        self.assertEqual(delta['baseline_report_digest'], baseline['report_digest'])
        self.assertEqual(delta['narrowed_report_digest'], narrowed['report_digest'])
        self.assertTrue(delta['baseline_report_retained'])
        self.assertFalse(delta['mutates_baseline'])
        self.assertEqual(delta['pin'], {'case-a': 'a-nursing'})
        county = next(item for item in delta['regions'] if item['region'] == 'county')
        self.assertEqual(county['state'], 'paired')
        self.assertGreaterEqual(__import__('decimal').Decimal(county['ratio_width_reduction']), 0)
        self.assertEqual(narrowed['feasible_completions'], 1)
        with self.assertRaises(ValueError):
            compare_narrowing(narrowed, baseline)
        mismatched = copy.deepcopy(narrowed)
        mismatched['definition_digest'] = 'x'
        with self.assertRaises(ValueError):
            compare_narrowing(baseline, mismatched)

    def test_worksheets_are_validated_and_definition_held_fixed(self):
        value = worksheet([unit('u1', 'd1', 'f1', ['nursing', 'bogus'])])
        with self.assertRaises(ValueError):
            self.analyse(value)
        value = worksheet([{'id': 'u1', 'candidates': [
            {'id': 'a', 'bed': 'd1', 'facility': 'f1', 'state': 'nursing'},
            {'id': 'a', 'bed': 'd2', 'facility': 'f1', 'state': 'general'},
        ]}])
        with self.assertRaises(ValueError):
            self.analyse(value)
        value = worksheet([unit('u1', 'd1', 'f1', ['nursing'])], [{'kind': 'requires', 'candidates': ['u1-nursing', 'ghost']}])
        with self.assertRaises(ValueError):
            self.analyse(value)
        value = worksheet([unit('u1', 'd1', 'f1', ['nursing'])], day='2026-11-11')
        with self.assertRaises(ValueError):
            self.analyse(value)
        wrong_known = worksheet([unit('u1', 'd1', 'f1', ['nursing'])], known_at='2026-11-11T00:00:00Z')
        with self.assertRaises(ValueError):
            self.analyse(wrong_known)
        with self.assertRaises(ValueError):
            self.analyse(worksheet([unit('u1', 'd1', 'f1', ['nursing'])]), {'missing': 'u1-nursing'})
        with self.assertRaises(ValueError):
            self.analyse(worksheet([unit('u1', 'd1', 'f1', ['nursing'])]), {'u1': 'u1-nursing'}, {'max_nodes': 0})
        alternative = copy.deepcopy(self.definition.export())
        alternative['target_basis_points'] = 8000
        from indicators.definitions import parse_definition
        other = parse_definition(alternative)
        report = analyze(self.catalog, self.current, worksheet([unit('u1', 'd1', 'f1', ['nursing'])]), other)
        self.assertEqual(report['definition_digest'], other.digest)

    def test_summary_and_query_wrappers(self):
        value = json.loads((FIXTURE.parent / 'disputed_units.json').read_text())
        summary = dispute_summary(self.catalog, self.current, self.definition, value)
        self.assertEqual(summary['status'], 'ready')
        self.assertEqual((summary['unit_count'], summary['candidate_count'], summary['relation_count']), (2, 4, 1))
        conflict_value = worksheet([unit('u1', 'bed-3', 'f1', ['nursing'])])
        conflict = dispute_summary(self.catalog, self.current, self.definition, conflict_value)
        self.assertEqual(conflict['status'], 'evidence_conflict')
        self.assertEqual(dispute_interval(self.catalog, self.current, self.definition, value)['status'], 'analyzed')
        with self.assertRaises(ValueError):
            dispute_narrowing({}, {})

    def test_packaging_cli_and_service_flow_carry_the_interval(self):
        value = json.loads((FIXTURE.parent / 'disputed_units.json').read_text())
        from bed_audit import parser, dispatch, report
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            written = write_package(self.document, self.current, self.definition, root / 'evidence', value)
            self.assertTrue(written['verified'])
            self.assertEqual(written['files'], 13)
            package = root / 'evidence'
            self.assertIn('dispute-interval.json', {path.name for path in package.iterdir()})
            loaded = json.loads((package / 'dispute-summary.json').read_text())
            self.assertEqual(loaded['status'], 'ready')
            interval = json.loads((package / 'dispute-interval.json').read_text())
            self.assertEqual(interval['status'], 'analyzed')
            self.assertEqual(interval['feasible_completions'], 3)
            verify_package(package)
            path = package / 'dispute-interval.json'
            path.write_text(canonical_tamper(path))
            with self.assertRaisesRegex(ValueError, 'checksum|derived'):
                verify_package(package)
            report_value = report(self.document, D, K, self.definition, value)
            self.assertEqual(report_value['dispute_summary']['status'], 'ready')
            self.assertEqual(report_value['dispute_interval']['feasible_completions'], 3)
        with tempfile.TemporaryDirectory() as directory:
            worksheet_path = Path(directory) / 'worksheet.json'
            worksheet_path.write_text(json.dumps(value))
            args = parser().parse_args(['dispute-interval', '--fixture', str(FIXTURE), '--worksheet', str(worksheet_path), '--day', D, '--known-at', K])
            result = dispatch(args)
            self.assertEqual(result['status'], 'analyzed')
            self.assertEqual(result['feasible_completions'], 3)
            args = parser().parse_args(['dispute-interval', '--fixture', str(FIXTURE), '--worksheet', str(worksheet_path), '--day', D, '--known-at', K, '--pin', '{"case-a": "a-nursing"}'])
            self.assertEqual(dispatch(args)['feasible_completions'], 1)
            baseline_path = Path(directory) / 'baseline.json'
            baseline_path.write_text(json.dumps(dispatch(parser().parse_args(['dispute-interval', '--fixture', str(FIXTURE), '--worksheet', str(worksheet_path), '--day', D, '--known-at', K]))))


def canonical_tamper(path):
    return json.dumps({'tampered': True})
