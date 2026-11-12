from collections import defaultdict
from decimal import Decimal
from indicators.compute import compute, measures
from indicators.definitions import parse_definition
from bed_events.replay import verify_snapshot
from registry.values import fingerprint, integer


def definition_comparison(catalog, current, definitions, level='region'):
    parsed = [parse_definition(value) for value in definitions]
    ids = [(item.id, item.version) for item in parsed]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate indicator definition identity')
    reports = [compute(catalog, current, item, level) for item in parsed]
    rows = []
    for report in reports:
        for row in report['rows']:
            rows.append({
                level: row[level],
                'definition_id': report['definition']['id'],
                'definition_version': report['definition']['version'],
                'definition_digest': report['definition_digest'],
                'numerator': row['numerator'],
                'denominator': row['denominator'],
                'ratio': row['ratio'],
                'meets_target': row['meets_target'],
            })
    return {'snapshot_digest': current['snapshot_digest'], 'level': level, 'rows': rows}


def facility_contributions(catalog, current, definition, region):
    if region not in catalog.region_map:
        raise KeyError(region)
    eligible_regions = set(catalog.descendants(region))
    facility_indicator = compute(catalog, current, definition, 'facility')
    visible = catalog.at(current['day'], current['known_at'])
    selected = [row for row in facility_indicator['rows'] if visible[row['facility']]['region'] in eligible_regions]
    total_numerator = sum(row['numerator'] for row in selected)
    total_denominator = sum(row['denominator'] for row in selected)
    overall = measures(total_numerator, total_denominator, definition)
    rows = []
    for row in selected:
        numerator_without = total_numerator - row['numerator']
        denominator_without = total_denominator - row['denominator']
        without = measures(numerator_without, denominator_without, definition)
        difference = None
        if overall['ratio'] is not None and without['ratio'] is not None:
            difference = format(Decimal(overall['ratio']) - Decimal(without['ratio']), '.12f')
        rows.append({
            **row,
            'share_of_regional_denominator': format(Decimal(row['denominator']) / Decimal(total_denominator), '.12f') if total_denominator else None,
            'ratio_without_facility': without['ratio'],
            'ratio_contribution_difference': difference,
        })
    return {'region': region, 'snapshot_digest': current['snapshot_digest'], 'overall': overall, 'facilities': rows, 'interpretation': 'descriptive_leave_one_facility_out_not_causal'}


def conversion_plan(catalog, current, definition, region, budgets):
    contributions = facility_contributions(catalog, current, definition, region)
    if not isinstance(budgets, list) or len(budgets) > 100:
        raise ValueError('expected bounded conversion budgets')
    for budget in budgets:
        integer(budget, 'budget', 0)
    total = contributions['overall']
    eligible_regions = set(catalog.descendants(region))
    candidates = [row for row in current['beds'] if row['region'] in eligible_regions and row['ownership'] in definition.ownerships and row['category'] in definition.denominator and row['category'] not in definition.numerator]
    candidates.sort(key=lambda row: (row['facility'], row['bed']))
    scenarios = []
    for budget in budgets:
        chosen = candidates[:budget]
        converted = len(chosen)
        after = measures(total['numerator'] + converted, total['denominator'], definition)
        scenarios.append({
            'budget_beds': budget,
            'convertible_beds': len(candidates),
            'converted': converted,
            'unused_budget_beds': budget - converted,
            'selected_beds': [row['bed'] for row in chosen],
            'after': after,
            'license_expansion_assumed': False,
            'operational_feasibility_not_assessed': True,
        })
    return {'region': region, 'snapshot_digest': current['snapshot_digest'], 'definition_digest': definition.digest, 'before': total, 'scenarios': scenarios}


def attribution_bridge(catalog, before, after, definition):
    verify_snapshot(before)
    verify_snapshot(after)
    a = {row['bed']: row for row in before['beds']}
    b = {row['bed']: row for row in after['beds']}
    groups = defaultdict(lambda: {'numerator': 0, 'denominator': 0, 'beds': []})
    def contribution(row):
        if row is None or row['ownership'] not in definition.ownerships or row['category'] not in definition.denominator:
            return 0, 0
        return int(row['category'] in definition.numerator), 1
    for bed in sorted(a.keys() | b.keys()):
        old, new = a.get(bed), b.get(bed)
        old_n, old_d = contribution(old)
        new_n, new_d = contribution(new)
        if old == new:
            continue
        if old is None:
            cause = 'entry_or_quality_resolution'
        elif new is None:
            cause = 'exit_or_quality_exclusion'
        elif old['category'] != new['category']:
            cause = 'classification_change'
        elif old['ownership'] != new['ownership']:
            cause = 'ownership_policy_change'
        elif old['region'] != new['region']:
            cause = 'regional_reattribution'
        else:
            cause = 'registry_revision_only'
        group = groups[cause]
        group['numerator'] += new_n - old_n
        group['denominator'] += new_d - old_d
        group['beds'].append(bed)
    old_total = [sum(contribution(row)[i] for row in a.values()) for i in (0, 1)]
    new_total = [sum(contribution(row)[i] for row in b.values()) for i in (0, 1)]
    if sum(g['numerator'] for g in groups.values()) != new_total[0] - old_total[0]:
        raise AssertionError('numerator bridge does not conserve')
    if sum(g['denominator'] for g in groups.values()) != new_total[1] - old_total[1]:
        raise AssertionError('denominator bridge does not conserve')
    return {'definition_digest': definition.digest, 'before': old_total, 'after': new_total, 'causes': [{'cause': key, **value} for key, value in sorted(groups.items())]}
