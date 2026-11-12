import copy
from decimal import Decimal
import unittest
from test_event_history import data, DAY, EARLY, LATEST
from bed_events.schema import parse_package
from bed_events.replay import snapshot
from indicators.definitions import nursing_definition, parse_definition
from indicators.compute import compute, measures, compare_indicators
from indicators.periods import period_indicator, daily_series, threshold_spells, change_boundaries
from indicators.disclosure import protect_regions, validate_protection
from indicators.portfolio import definition_comparison, facility_contributions, conversion_plan, attribution_bridge
from indicators.flows import stock_flow_bridge, daily_capacity_utilization
from audit.quality import check_additivity, trace_coverage
from audit.claims import reconcile_claims, verify_reported_ratios


class IndicatorTests(unittest.TestCase):
    def setUp(self):
        self.document = data()
        self.catalog, self.events = parse_package(self.document)
        self.current = snapshot(self.catalog, self.events, DAY, LATEST)
        self.definition = nursing_definition()

    def test_denominator_is_unique_active_beds_and_aggregate_is_weighted(self):
        report = compute(self.catalog, self.current, self.definition)
        rows = {row['region']: row for row in report['rows']}
        self.assertEqual((rows['county']['numerator'], rows['county']['denominator']), (2, 5))
        self.assertEqual(rows['county']['ratio'], '0.400000000000')
        self.assertEqual(rows['town']['ratio'], '0.333333333333')
        self.assertEqual(rows['village']['ratio'], '0.500000000000')
        self.assertNotEqual(Decimal(rows['county']['ratio']), (Decimal(rows['town']['ratio']) + Decimal(rows['village']['ratio'])) / 2)
        self.assertTrue(check_additivity(self.catalog, report)['valid'])
        self.assertTrue(trace_coverage(self.current)['valid'])

    def test_conversion_gap_differs_from_added_nursing_gap(self):
        row = measures(2, 5, self.definition)
        self.assertEqual(row['conversion_gap'], 2)
        self.assertEqual(row['additional_nursing_gap'], 7)
        self.assertTrue(measures(4, 5, self.definition)['meets_target'])
        self.assertFalse(measures(3, 5, self.definition)['meets_target'])
        added = row['additional_nursing_gap']
        self.assertTrue(measures(2 + added, 5 + added, self.definition)['meets_target'])
        self.assertFalse(measures(1 + added, 4 + added, self.definition)['meets_target'])

    def test_exact_threshold_zero_and_unattainable_addition(self):
        value = self.definition.export()
        value['target_basis_points'] = 10000
        definition = parse_definition(value)
        self.assertIsNone(measures(2, 5, definition)['additional_nursing_gap'])
        self.assertEqual(measures(5, 5, definition)['additional_nursing_gap'], 0)
        empty = measures(0, 0, definition)
        self.assertIsNone(empty['ratio'])
        self.assertIsNone(empty['meets_target'])
        self.assertIsNone(empty['conversion_gap'])
        with self.assertRaises(ValueError):
            measures(3, 2, definition)
        value['target_basis_points'] = 0
        self.assertTrue(measures(0, 5, parse_definition(value))['meets_target'])

    def test_definition_filters_ownership_and_category_in_both_counts(self):
        value = self.definition.export()
        value['ownerships'] = ['public']
        value['denominator'] = ['nursing', 'general']
        definition = parse_definition(value)
        report = compute(self.catalog, self.current, definition)
        root = next(row for row in report['rows'] if row['region'] == 'county')
        self.assertEqual((root['numerator'], root['denominator']), (1, 2))
        self.assertEqual(len(report['excluded_by_policy']), 3)
        facility = compute(self.catalog, self.current, definition, 'facility')
        self.assertEqual([row['facility'] for row in facility['rows']], ['f1'])
        ownership = compute(self.catalog, self.current, definition, 'ownership')
        self.assertEqual(ownership['rows'][0]['denominator'], 2)

    def test_invalid_and_duplicate_definition_members_rejected(self):
        mutations = [
            {'numerator': ['nursing', 'nursing']},
            {'denominator': ['general']},
            {'ownerships': []},
            {'ownerships': ['unknown']},
            {'target_basis_points': 10001},
            {'version': True},
        ]
        for mutation in mutations:
            value = {**self.definition.export(), **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                parse_definition(value)
        with self.assertRaises(ValueError):
            compute(self.catalog, self.current, self.definition, 'unknown')

    def test_daily_bed_days_match_segment_sweep(self):
        start, end = '2026-11-01', '2026-11-06'
        period = period_indicator(self.catalog, self.events, start, end, LATEST, self.definition)
        series = daily_series(self.catalog, self.events, start, end, LATEST, self.definition)
        daily_root = [row for row in series['rows'] if row['region'] == 'county']
        period_root = next(row for row in period['rows'] if row['region'] == 'county')
        self.assertEqual(period['days'], 5)
        self.assertEqual(sum(row['denominator'] for row in daily_root), period_root['denominator_bed_days'])
        self.assertEqual(sum(row['numerator'] for row in daily_root), period_root['numerator_bed_days'])
        self.assertEqual(sum(row['days'] for row in period['segments']), 5)
        self.assertEqual(period_root['denominator_bed_days'], 26)
        self.assertEqual(period_root['numerator_bed_days'], 8)
        self.assertEqual(period_root['days_below_target'], 5)
        boundaries = change_boundaries(self.catalog, self.events, start, end, LATEST)
        self.assertEqual(boundaries, ['2026-11-01', '2026-11-03', '2026-11-04', '2026-11-06'])

    def test_threshold_spells_do_not_join_missing_days(self):
        series = daily_series(self.catalog, self.events, '2026-11-01', '2026-11-06', LATEST, self.definition)
        spells = threshold_spells(series, 'region')
        county = [row for row in spells if row['region'] == 'county']
        self.assertEqual(county[0]['days'], 5)
        series['rows'] = [row for row in series['rows'] if row['day'] != '2026-11-03']
        county = [row for row in threshold_spells(series, 'region') if row['region'] == 'county']
        self.assertEqual([row['days'] for row in county], [2, 2])

    def test_small_cells_suppress_ancestors_to_prevent_complement(self):
        indicator = compute(self.catalog, self.current, self.definition)
        protected = protect_regions(self.catalog, indicator)
        rows = {row['region']: row for row in protected['rows']}
        self.assertTrue(rows['town']['suppressed'])
        self.assertTrue(rows['village']['suppressed'])
        self.assertTrue(rows['county']['suppressed'])
        self.assertEqual(rows['county']['suppression_reason'], 'descendant_complement_protection')
        self.assertIsNone(rows['county']['denominator'])
        self.assertTrue(validate_protection(self.catalog, protected))
        public = protect_regions(self.catalog, indicator, threshold=2)
        self.assertFalse(any(row['suppressed'] for row in public['rows']))

    def test_disclosure_validator_catches_leaking_parent_and_measures(self):
        indicator = compute(self.catalog, self.current, self.definition)
        public = protect_regions(self.catalog, indicator)
        altered = copy.deepcopy(public)
        next(row for row in altered['rows'] if row['region'] == 'town')['numerator'] = 1
        with self.assertRaisesRegex(ValueError, 'sensitive'):
            validate_protection(self.catalog, altered)
        altered = copy.deepcopy(public)
        next(row for row in altered['rows'] if row['region'] == 'county')['suppressed'] = False
        with self.assertRaises(ValueError):
            validate_protection(self.catalog, altered)
        with self.assertRaises(ValueError):
            protect_regions(self.catalog, compute(self.catalog, self.current, self.definition, 'facility'))

    def test_compare_definitions_refuses_incompatible_ratios(self):
        old = compute(self.catalog, snapshot(self.catalog, self.events, DAY, EARLY), self.definition)
        new = compute(self.catalog, self.current, self.definition)
        delta = compare_indicators(old, new)
        root = next(row for row in delta['rows'] if row['region'] == 'county')
        self.assertEqual(root['numerator_change'], -1)
        self.assertEqual(root['denominator_change'], 0)
        alternative = self.definition.export()
        alternative['ownerships'] = ['public']
        alternative['version'] = 2
        incompatible = compute(self.catalog, self.current, parse_definition(alternative))
        with self.assertRaises(ValueError):
            compare_indicators(old, incompatible)
        matrix = definition_comparison(self.catalog, self.current, [self.definition.export(), alternative])
        self.assertEqual(len(matrix['rows']), 6)
        with self.assertRaises(ValueError):
            definition_comparison(self.catalog, self.current, [alternative, alternative])

    def test_facility_contributions_are_descriptive_and_weighted(self):
        value = facility_contributions(self.catalog, self.current, self.definition, 'county')
        self.assertEqual(value['overall']['denominator'], 5)
        self.assertEqual(sum(row['denominator'] for row in value['facilities']), 5)
        shares = [Decimal(row['share_of_regional_denominator']) for row in value['facilities']]
        self.assertEqual(sum(shares), Decimal(1))
        f1 = next(row for row in value['facilities'] if row['facility'] == 'f1')
        self.assertEqual(f1['ratio_without_facility'], '0.500000000000')
        self.assertEqual(f1['ratio_contribution_difference'], '-0.100000000000')
        self.assertIn('not_causal', value['interpretation'])

    def test_conversion_scenarios_do_not_invent_eligible_supply(self):
        plan = conversion_plan(self.catalog, self.current, self.definition, 'county', [0, 2, 100])
        self.assertEqual(plan['scenarios'][0]['after']['numerator'], 2)
        self.assertEqual(plan['scenarios'][1]['after']['numerator'], 4)
        self.assertTrue(plan['scenarios'][1]['after']['meets_target'])
        self.assertEqual(plan['scenarios'][2]['converted'], 3)
        self.assertEqual(plan['scenarios'][2]['unused_budget_beds'], 97)
        self.assertEqual(plan['scenarios'][2]['after']['denominator'], 5)
        self.assertTrue(plan['scenarios'][2]['operational_feasibility_not_assessed'])

    def test_stock_bridge_and_attribution_conserve_deltas(self):
        bridge = stock_flow_bridge(self.catalog, self.events, DAY, '2026-11-05', LATEST, self.definition)
        county = next(row for row in bridge['rows'] if row['region'] == 'county')
        self.assertEqual(county['opening_denominator'], 5)
        self.assertEqual(county['denominator_entries'], 1)
        self.assertEqual(county['denominator_exits'], 1)
        self.assertEqual(county['closing_denominator'], 5)
        before = snapshot(self.catalog, self.events, DAY, EARLY)
        attribution = attribution_bridge(self.catalog, before, self.current, self.definition)
        self.assertEqual(sum(row['numerator'] for row in attribution['causes']), -1)
        self.assertEqual(sum(row['denominator'] for row in attribution['causes']), 0)
        utilization = daily_capacity_utilization(self.catalog, self.events, DAY, '2026-11-03', LATEST)
        self.assertEqual(len(utilization['rows']), 4)
        self.assertTrue(all(row['over_license_beds'] == 0 for row in utilization['rows']))

    def test_claim_reconciliation_and_ratio_arithmetic(self):
        claims = [{'facility': 'f1', 'category': 'nursing', 'count': 2}, {'facility': 'unknown', 'category': 'general', 'count': 1}]
        reconciled = reconcile_claims(self.catalog, self.current, claims)
        self.assertFalse(reconciled['fully_reconciled'])
        self.assertTrue(any(row['state'] == 'unknown_facility' for row in reconciled['rows']))
        self.assertTrue(any(row['state'] == 'missing_claim' for row in reconciled['rows']))
        nursing = next(row for row in reconciled['rows'] if row['facility'] == 'f1' and row['category'] == 'nursing')
        self.assertEqual(nursing['difference'], 1)
        report = compute(self.catalog, self.current, self.definition)
        declared = [{'region': row['region'], 'numerator': row['numerator'], 'denominator': row['denominator'], 'ratio': row['ratio']} for row in report['rows']]
        self.assertTrue(verify_reported_ratios(report, declared)['valid'])
        declared[0]['ratio'] = '0.99'
        self.assertIn('ratio_arithmetic_mismatch', verify_reported_ratios(report, declared)['rows'][0]['reasons'])
        with self.assertRaises(ValueError):
            reconcile_claims(self.catalog, self.current, claims + claims)

    def test_direct_parent_facilities_do_not_disappear_from_region_total(self):
        changed = data()
        changed['facilities'][0]['region'] = 'county'
        catalog, events = parse_package(changed)
        current = snapshot(catalog, events, DAY, LATEST)
        indicator = compute(catalog, current, self.definition)
        rows = {row['region']: row for row in indicator['rows']}
        self.assertEqual(rows['county']['denominator'], 5)
        self.assertEqual(rows['town']['denominator'], 0)
        self.assertEqual(rows['village']['denominator'], 2)
        self.assertTrue(check_additivity(catalog, indicator)['valid'])
        self.assertIsNone(rows['town']['ratio'])
        self.assertFalse(rows['town']['small_denominator'])
        protected = protect_regions(catalog, indicator)
        visible_town = next(row for row in protected['rows'] if row['region'] == 'town')
        self.assertFalse(visible_town['suppressed'])
        self.assertEqual(visible_town['denominator'], 0)

    def test_unlicensed_supply_is_excluded_without_becoming_zero_observation(self):
        changed = data()
        changed['facilities'][0]['licensed'] = False
        catalog, events = parse_package(changed)
        current = snapshot(catalog, events, DAY, LATEST)
        self.assertEqual(len(current['excluded']), 3)
        self.assertEqual(len(current['beds']), 2)
        self.assertTrue(all(row['reason'] == 'facility_unlicensed' for row in current['excluded']))
        indicator = compute(catalog, current, self.definition)
        root = next(row for row in indicator['rows'] if row['region'] == 'county')
        self.assertEqual((root['numerator'], root['denominator']), (1, 2))
        self.assertEqual(indicator['quality']['excluded_beds'], 3)
        facility = compute(catalog, current, self.definition, 'facility')
        self.assertEqual([row['facility'] for row in facility['rows']], ['f2'])
        self.assertTrue(current['publishable'])
