"""Bounded evidence analysis for manually curated disputed bed units.

The worksheet is deliberately separate from the official event ledger.  This
module never writes hypotheses back into ``registry.json`` or the ledger.  It
only answers how far the presently visible evidence can support a regional
nursing share, and returns replayable candidate combinations for the two
endpoints of every region.

The confirmed active snapshot is the fixed base.  Every disputed unit
contributes exactly one mutually exclusive candidate state.  A candidate is
one of ``nursing``, ``general``, ``rehabilitation`` or ``absent``; absent
removes the bed from both counts.  Shared facility license capacity and the
unit relations (``excludes``, ``requires``, ``same_case``) are enforced by a
depth-first enumeration over the global product of units, so parent regions
are solved jointly with their children rather than by adding child extrema.
"""

import copy
import json
from collections import defaultdict
from decimal import Decimal

from registry.values import object_fields, identifier, iso_day, stamp, bounded_list, fingerprint, decimal_ratio
from bed_events.replay import verify_snapshot
from indicators.compute import compute, measures


STATES = {'nursing', 'general', 'rehabilitation', 'absent'}
RELATION_KINDS = {'excludes', 'requires', 'same_case'}
DEFAULT_LIMITS = {'max_completions': 20000, 'max_nodes': 500000}
MAX_UNITS = 2000
MAX_CANDIDATES_PER_UNIT = 100
MAX_RELATIONS = 10000


def _limits(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or value.keys() - set(DEFAULT_LIMITS):
        raise ValueError('limits may only contain max_completions and max_nodes')
    result = dict(DEFAULT_LIMITS)
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 10 ** 9:
            raise ValueError('invalid resource limit: ' + key)
        result[key] = raw
    return result


def parse_candidate(row, path):
    object_fields(row, ('id', 'bed', 'facility', 'state'), ('region',), path=path)
    identifier(row['id'], path + '.id')
    identifier(row['bed'], path + '.bed')
    identifier(row['facility'], path + '.facility')
    if row['state'] not in STATES:
        raise ValueError(path + '.state: unknown candidate state')
    if 'region' in row:
        identifier(row['region'], path + '.region')
    return {
        'id': row['id'],
        'bed': row['bed'],
        'facility': row['facility'],
        'state': row['state'],
        'region': row.get('region'),
    }


def parse_worksheet(value):
    object_fields(value, ('day', 'known_at', 'units'), ('relations',), path='worksheet')
    iso_day(value['day'], 'worksheet.day')
    stamp(value['known_at'], 'worksheet.known_at')
    bounded_list(value['units'], 'worksheet.units', MAX_UNITS)
    relations_value = value.get('relations', [])
    if not isinstance(relations_value, list) or len(relations_value) > MAX_RELATIONS:
        raise ValueError('worksheet.relations must be a bounded list')
    units = []
    unit_ids = set()
    candidate_ids = set()
    candidate_units = {}
    for index, row in enumerate(value['units']):
        path = f'worksheet.units[{index}]'
        object_fields(row, ('id', 'candidates'), path=path)
        identifier(row['id'], path + '.id')
        if row['id'] in unit_ids:
            raise ValueError('duplicate dispute unit id: ' + row['id'])
        unit_ids.add(row['id'])
        bounded_list(row['candidates'], path + '.candidates', MAX_CANDIDATES_PER_UNIT)
        if not row['candidates']:
            raise ValueError(path + '.candidates must not be empty')
        candidates = []
        for candidate_index, raw in enumerate(row['candidates']):
            candidate = parse_candidate(raw, f'{path}.candidates[{candidate_index}]')
            if candidate['id'] in candidate_ids:
                raise ValueError('duplicate candidate id: ' + candidate['id'])
            candidate_ids.add(candidate['id'])
            candidate_units[candidate['id']] = row['id']
            candidates.append(candidate)
        candidates.sort(key=lambda item: item['id'])
        units.append({'id': row['id'], 'candidates': candidates})
    units.sort(key=lambda item: item['id'])
    relations = []
    seen_relations = set()
    for index, row in enumerate(relations_value):
        path = f'worksheet.relations[{index}]'
        object_fields(row, ('kind', 'candidates'), path=path)
        if row['kind'] not in RELATION_KINDS:
            raise ValueError(path + '.kind: unknown relation kind')
        bounded_list(row['candidates'], path + '.candidates', MAX_CANDIDATES_PER_UNIT)
        members = list(row['candidates'])
        if any(not isinstance(item, str) for item in members):
            raise ValueError(path + '.candidates must be strings')
        if len(members) < 2 or len(members) != len(set(members)):
            raise ValueError(path + '.candidates must be at least two unique candidates')
        if row['kind'] == 'requires' and len(members) != 2:
            raise ValueError(path + ': requires relation takes an ordered pair')
        for member in members:
            if member not in candidate_units:
                raise ValueError(path + ': unknown candidate ' + member)
        if len({candidate_units[member] for member in members}) == 1:
            raise ValueError(path + ': relation must connect candidates from distinct units')
        key = (row['kind'], tuple(members))
        if key in seen_relations:
            raise ValueError(path + ': duplicate relation')
        seen_relations.add(key)
        relations.append({'kind': row['kind'], 'candidates': members})
    relations.sort(key=lambda item: (item['kind'], item['candidates']))
    return {
        'day': value['day'],
        'known_at': value['known_at'],
        'units': units,
        'relations': relations,
        'candidate_units': candidate_units,
        'digest': fingerprint({'day': value['day'], 'known_at': value['known_at'], 'units': units, 'relations': relations}),
    }


def _sorted_conflict_key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def evidence_conflicts(catalog, current, worksheet):
    """Identity/region/base-capacity incompatibilities, detected before solving.

    No conflict is repaired by guessing: licenses are never expanded and no
    candidate event is ever applied to the official ledger.
    """
    result = []
    facilities = catalog.at(current['day'], current['known_at'])
    base_partitions = {}
    for partition in ('beds', 'retired', 'quarantined', 'excluded'):
        for row in current[partition]:
            base_partitions.setdefault(row['bed'], partition)
    beds_by_unit = defaultdict(set)
    candidate_ids_by_bed = defaultdict(list)
    for unit in worksheet['units']:
        for candidate in unit['candidates']:
            beds_by_unit[candidate['bed']].add(unit['id'])
            candidate_ids_by_bed[candidate['bed']].append(candidate['id'])
            if candidate['bed'] in base_partitions:
                result.append({
                    'code': 'candidate_bed_conflicts_with_known_bed',
                    'unit': unit['id'],
                    'candidate': candidate['id'],
                    'bed': candidate['bed'],
                    'snapshot_partition': base_partitions[candidate['bed']],
                })
            if candidate['facility'] not in catalog.facility_versions:
                result.append({'code': 'unknown_facility', 'unit': unit['id'], 'candidate': candidate['id'], 'facility': candidate['facility']})
                continue
            facility = facilities.get(candidate['facility'])
            if facility is None:
                result.append({'code': 'facility_not_effective_or_not_known', 'unit': unit['id'], 'candidate': candidate['id'], 'facility': candidate['facility']})
                continue
            if candidate['state'] != 'absent':
                if not facility['licensed']:
                    result.append({'code': 'facility_unlicensed', 'unit': unit['id'], 'candidate': candidate['id'], 'facility': candidate['facility']})
                if candidate['region'] is not None and candidate['region'] != facility['region']:
                    result.append({
                        'code': 'candidate_region_incompatible',
                        'unit': unit['id'],
                        'candidate': candidate['id'],
                        'facility': candidate['facility'],
                        'declared_region': candidate['region'],
                        'facility_region': facility['region'],
                    })
    for bed, unit_ids in sorted(beds_by_unit.items()):
        if len(unit_ids) > 1:
            result.append({
                'code': 'candidate_bed_shared_across_units',
                'bed': bed,
                'units': sorted(unit_ids),
                'candidates': sorted(candidate_ids_by_bed[bed]),
            })
    base_counts = defaultdict(int)
    for row in current['beds']:
        base_counts[row['facility']] += 1
    for facility_id, count in sorted(base_counts.items()):
        facility = facilities[facility_id]
        if count > facility['license_limit']:
            result.append({
                'code': 'confirmed_base_exceeds_license',
                'facility': facility_id,
                'confirmed_beds': count,
                'license_limit': facility['license_limit'],
            })
    result.sort(key=lambda item: (item['code'], _sorted_conflict_key(item)))
    return result


def _base_region_counts(catalog, current, definition):
    indicator = compute(catalog, current, definition)
    rows = {row['region']: {'numerator': row['numerator'], 'denominator': row['denominator']} for row in indicator['rows']}
    for region in catalog.region_map:
        rows.setdefault(region, {'numerator': 0, 'denominator': 0})
    return rows


def _candidate_contributions(catalog, worksheet, facilities, definition):
    result = {}
    for unit in worksheet['units']:
        for candidate in unit['candidates']:
            facility = facilities[candidate['facility']]
            region = candidate['region'] if candidate['region'] is not None else facility['region']
            if candidate['state'] != 'absent' and facility['ownership'] in definition.ownerships and candidate['state'] in definition.denominator:
                numerator, denominator = int(candidate['state'] in definition.numerator), 1
            else:
                numerator, denominator = 0, 0
            ancestry = [(ancestor, numerator, denominator) for ancestor in catalog.ancestors(region)]
            result[candidate['id']] = {
                'unit': unit['id'],
                'candidate': candidate,
                'region': region,
                'facility_row': facility,
                'ancestors': ancestry,
                'numerator': numerator,
                'denominator': denominator,
                'license_units': 0 if candidate['state'] == 'absent' else 1,
            }
    return result


def relation_satisfied(selected_ids, worksheet, assigned_units, complete):
    for relation in worksheet['relations']:
        members = relation['candidates']
        units = {worksheet['candidate_units'][member] for member in members}
        kind = relation['kind']
        if kind == 'excludes':
            if sum(member in selected_ids for member in members) > 1:
                return False
        elif kind == 'requires':
            first, second = members
            if first in selected_ids and (complete or worksheet['candidate_units'][second] in assigned_units):
                if second not in selected_ids:
                    return False
        elif kind == 'same_case':
            present = [member for member in members if member in selected_ids]
            if present and (complete or units <= assigned_units):
                if len(present) != len(members):
                    return False
    return True


def enumerate_completions(worksheet, contributions, base_counts, facilities, pinned, limits):
    """Depth-first enumeration of one candidate per unit.

    Returns ``(completions, truncated, nodes)``.  Capacity is enforced per
    facility on the confirmed base plus selected non-absent candidates.
    """
    units = worksheet['units']
    unit_ids = [unit['id'] for unit in units]
    options = []
    for unit in units:
        if unit['id'] in pinned:
            chosen = [candidate for candidate in unit['candidates'] if candidate['id'] == pinned[unit['id']]]
            options.append(chosen)
        else:
            options.append(list(unit['candidates']))
    state = {'nodes': 0, 'truncated': False}
    assignments = {}
    selected_ids = set()
    facility_counts = dict(base_counts)
    completions = []
    index = [0] * len(units)
    depth = 0
    while depth >= 0 and not state['truncated']:
        if depth == len(units):
            completions.append(tuple(assignments[unit] for unit in unit_ids))
            if len(completions) >= limits['max_completions']:
                state['truncated'] = True
                break
            if depth == 0:
                break
            last = units[depth - 1]['id']
            removed = assignments.pop(last)
            selected_ids.discard(removed)
            facility_counts[contributions[removed]['candidate']['facility']] -= contributions[removed]['license_units']
            index[depth - 1] += 1
            depth -= 1
            continue
        advanced = False
        while index[depth] < len(options[depth]):
            state['nodes'] += 1
            if state['nodes'] > limits['max_nodes']:
                state['truncated'] = True
                break
            candidate = options[depth][index[depth]]
            contribution = contributions[candidate['id']]
            facility_id = candidate['facility']
            next_count = facility_counts.get(facility_id, 0) + contribution['license_units']
            if next_count > facilities[facility_id]['license_limit']:
                index[depth] += 1
                continue
            assignments[units[depth]['id']] = candidate['id']
            selected_ids.add(candidate['id'])
            facility_counts[facility_id] = next_count
            if relation_satisfied(selected_ids, worksheet, set(unit_ids[:depth + 1]), False):
                depth += 1
                if depth < len(units):
                    index[depth] = 0
                advanced = True
                break
            selected_ids.discard(candidate['id'])
            facility_counts[facility_id] -= contribution['license_units']
            assignments.pop(units[depth]['id'])
            index[depth] += 1
        if state['truncated']:
            break
        if not advanced:
            index[depth] = 0
            if depth > 0:
                removed = assignments.pop(units[depth - 1]['id'])
                selected_ids.discard(removed)
                facility_counts[contributions[removed]['candidate']['facility']] -= contributions[removed]['license_units']
                index[depth - 1] += 1
            depth -= 1
    if not state['truncated']:
        for completion in completions:
            if not relation_satisfied(set(completion), worksheet, set(unit_ids), True):
                raise AssertionError('enumerated completion violates a relation')
    return completions, state['truncated'], state['nodes']


def outer_bounds(worksheet, contributions, base_region, region, target):
    """Sound per-region outer bounds that ignore capacity and relations.

    Each unit selects at most one candidate, so the extremes are additive over
    units.  Numerators and denominators are relaxed independently; the
    ``ratio_lower_bound`` divides the independent numerator lower bound by the
    independent denominator upper bound and is therefore only an outer bound,
    never presented as an exact endpoint.
    """
    numerator_low = numerator_high = base_region['numerator']
    denominator_low = denominator_high = base_region['denominator']
    for unit in worksheet['units']:
        nums, dens = [], []
        for candidate in unit['candidates']:
            found = next((value for ancestor, value in ((name, (n, d)) for name, n, d in contributions[candidate['id']]['ancestors']) if ancestor == region), (0, 0))
            nums.append(found[0])
            dens.append(found[1])
        numerator_low += min(nums)
        numerator_high += max(nums)
        denominator_low += min(dens)
        denominator_high += max(dens)
    ratio_lower = decimal_ratio(numerator_low, denominator_high) if denominator_high > 0 else None
    ratio_upper = decimal_ratio(numerator_high, denominator_low) if denominator_low > 0 else None
    required_low = (denominator_low * target + 9999) // 10000 if denominator_low > 0 else None
    required_high = (denominator_high * target + 9999) // 10000 if denominator_high > 0 else None
    return {
        'numerator_range': [numerator_low, numerator_high],
        'denominator_range': [denominator_low, denominator_high],
        'ratio_lower_bound': ratio_lower,
        'ratio_upper_bound': ratio_upper,
        'conversion_lower_bound': None if required_low is None else max(0, required_low - numerator_high),
        'conversion_upper_bound': None if required_high is None else max(0, required_high - numerator_low),
        'certain_meets': denominator_low > 0 and numerator_low * 10000 >= denominator_high * target,
        'certain_misses': numerator_high * 10000 < denominator_low * target,
        'method': 'independent_additive_relaxation_not_exact',
        'tight': False,
    }


def _ratio_cmp(numerator_a, denominator_a, numerator_b, denominator_b):
    left, right = numerator_a * denominator_b, numerator_b * denominator_a
    return (left > right) - (left < right)


def build_witness(catalog, current, worksheet, completion, contributions, base_region, facilities):
    selected = []
    region_counts = {region: dict(counts) for region, counts in base_region.items()}
    facility_counts = defaultdict(int)
    for row in current['beds']:
        facility_counts[row['facility']] += 1
    for candidate_id in completion:
        contribution = contributions[candidate_id]
        candidate = contribution['candidate']
        selected.append({
            'unit': contribution['unit'],
            'candidate': candidate_id,
            'bed': candidate['bed'],
            'facility': candidate['facility'],
            'state': candidate['state'],
            'region': contribution['region'],
            'counts_in_denominator': contribution['denominator'],
            'counts_in_numerator': contribution['numerator'],
        })
        facility_counts[candidate['facility']] += contribution['license_units']
        for region, numerator, denominator in contribution['ancestors']:
            region_counts[region]['numerator'] += numerator
            region_counts[region]['denominator'] += denominator
    selected.sort(key=lambda item: (item['unit'], item['candidate']))
    usage = {}
    for facility_id, count in sorted(facility_counts.items()):
        facility = facilities[facility_id]
        usage[facility_id] = {
            'beds': count,
            'license_limit': facility['license_limit'],
            'within_limit': count <= facility['license_limit'],
        }
    body = {
        'selected': selected,
        'regional_counts': {region: region_counts[region] for region in sorted(region_counts)},
        'license_usage': usage,
    }
    return {**body, 'completion_digest': fingerprint(body)}


def _conclusion(stats, truncated, relaxation):
    """Decide the three-state conclusion.

    A ``certain`` conclusion is only emitted when it is proven either by a
    complete enumeration or, after truncation, by the sound outer relaxation.
    Otherwise the report keeps ``inconclusive`` and marks the result partial.
    """
    defined = stats['defined'] > 0
    meets_all = defined and stats['meets'] == stats['defined']
    misses_all = defined and stats['meets'] == 0
    if not truncated:
        if stats['evaluated'] == 0:
            return 'inconclusive', 'no_feasible_completion', True
        if not defined:
            return 'inconclusive', 'only_zero_denominator_states', True
        if meets_all:
            return 'certain_meets', 'all_feasible_defined_states_meet', True
        if misses_all:
            return 'certain_misses', 'all_feasible_defined_states_miss', True
        return 'inconclusive', 'feasible_states_disagree', True
    if truncated and stats['evaluated'] == 0:
        # No state witnessed yet: a vacuous "all states meet/miss" must not
        # be reported as a proven conclusion.
        return 'inconclusive', 'resource_limit_reached_before_any_state', False
    if relaxation['certain_meets']:
        return 'certain_meets', 'outer_bounds_prove_all_feasible_states_meet', True
    if relaxation['certain_misses']:
        return 'certain_misses', 'outer_bounds_prove_no_feasible_state_meets', True
    if meets_all or misses_all:
        # Witnessed on the truncated prefix only: no proof for the remainder.
        return 'inconclusive', 'resource_limit_witnessed_prefix_only', False
    return 'inconclusive', 'resource_limit_reached_before_full_enumeration', False


def _region_report(catalog, region, base_region, stats, definition, truncated, relaxation):
    conclusion, reason, proven = _conclusion(stats, truncated, relaxation)
    defined = stats['defined'] > 0
    return {
        'region': region,
        'base': copy.deepcopy(base_region),
        'parent': catalog.region_map[region]['parent'],
        'children': sorted(ident for ident in catalog.region_map if catalog.region_map[ident]['parent'] == region),
        'target_basis_points': definition.target_basis_points,
        'evaluated_completions': stats['evaluated'],
        'defined_denominator_completions': stats['defined'],
        'numerator_range': list(stats['numerator_range']) if defined else [None, None],
        'denominator_range': list(stats['denominator_range']) if defined else [None, None],
        'ratio': {
            'min': stats['ratio_min'],
            'max': stats['ratio_max'],
            'min_witness': stats['ratio_min_witness'],
            'max_witness': stats['ratio_max_witness'],
        },
        'conversion_needed': {
            'min': stats['conversion_min'],
            'max': stats['conversion_max'],
            'min_witness': stats['conversion_min_witness'],
            'max_witness': stats['conversion_max_witness'],
        },
        'meets_target': {
            'all_defined_states_meet': defined and stats['meets'] == stats['defined'],
            'any_defined_state_meets': stats['meets'] > 0,
            'defined_states': stats['defined'],
            'undefined_states_excluded': stats['zero'],
        },
        'conclusion': conclusion,
        'conclusion_reason': reason,
        'conclusion_proven': proven,
        'zero_denominator': {
            'completion_count': stats['zero'],
            'only_state': stats['evaluated'] > 0 and stats['zero'] == stats['evaluated'],
            'first_witness': stats['zero_witness'],
            'excluded_from_ratio_extrema': True,
        },
        'extrema_proven': not truncated,
        'witnessed_extrema_only': truncated,
        'outer_bounds': copy.deepcopy(relaxation),
        'parent_extrema_jointly_solved': True,
    }


def analyze(catalog, current, worksheet, definition, pin=None, limits=None):
    verify_snapshot(current)
    parsed = parse_worksheet(worksheet)
    limits = _limits(limits)
    if parsed['day'] != current['day']:
        raise ValueError('worksheet business day does not match snapshot day')
    if parsed['known_at'] != current['known_at']:
        raise ValueError('worksheet known_at does not match snapshot knowledge time')
    pin = {} if pin is None else dict(pin)
    for unit in parsed['units']:
        if unit['id'] in pin and not any(candidate['id'] == pin[unit['id']] for candidate in unit['candidates']):
            raise ValueError('pinned candidate does not belong to unit ' + unit['id'])
    unknown = set(pin) - {unit['id'] for unit in parsed['units']}
    if unknown:
        raise ValueError('pin references unknown units: ' + ','.join(sorted(unknown)))
    facilities = catalog.at(current['day'], current['known_at'])
    header = {
        'analysis': 'disputed-evidence-interval',
        'version': 1,
        'day': current['day'],
        'known_at': current['known_at'],
        'snapshot_digest': current['snapshot_digest'],
        'worksheet_digest': parsed['digest'],
        'definition': definition.export(),
        'definition_digest': definition.digest,
        'pin': dict(sorted(pin.items())),
        'limits': limits,
        'witness_policy': 'lexicographically_first_completion_at_each_extreme',
        'hypotheses_are_not_written_to_events': True,
    }
    conflicts = evidence_conflicts(catalog, current, parsed)
    if conflicts:
        report = {
            **header,
            'status': 'evidence_conflict',
            'analysis_performed': False,
            'conflicts': conflicts,
            'feasible_completions': 0,
            'truncated': False,
            'nodes_evaluated': 0,
            'witnesses': {},
            'regions': [],
            'zero_denominator_completions': 0,
            'license_expansion_assumed': False,
        }
        report['report_digest'] = fingerprint(report)
        return report
    base_region = _base_region_counts(catalog, current, definition)
    contributions = _candidate_contributions(catalog, parsed, facilities, definition)
    base_counts = defaultdict(int)
    for row in current['beds']:
        base_counts[row['facility']] += 1
    completions, truncated, nodes = enumerate_completions(parsed, contributions, base_counts, facilities, pin, limits)
    stats = {
        region: {
            'evaluated': 0,
            'defined': 0,
            'zero': 0,
            'meets': 0,
            'numerator_range': None,
            'denominator_range': None,
            'ratio_min': None,
            'ratio_max': None,
            'ratio_min_key': None,
            'ratio_max_key': None,
            'ratio_min_witness': None,
            'ratio_max_witness': None,
            'conversion_min': None,
            'conversion_max': None,
            'conversion_min_key': None,
            'conversion_max_key': None,
            'conversion_min_witness': None,
            'conversion_max_witness': None,
            'zero_witness': None,
            'zero_completion': None,
        }
        for region in catalog.region_map
    }
    completions_with_zero = 0
    for completion in completions:
        region_counts = {region: dict(value) for region, value in base_region.items()}
        for candidate_id in completion:
            for region, numerator, denominator in contributions[candidate_id]['ancestors']:
                region_counts[region]['numerator'] += numerator
                region_counts[region]['denominator'] += denominator
        has_zero = False
        for region, counts in region_counts.items():
            state = stats[region]
            state['evaluated'] += 1
            numerator, denominator = counts['numerator'], counts['denominator']
            if denominator == 0:
                has_zero = True
                state['zero'] += 1
                if state['zero_completion'] is None:
                    state['zero_completion'] = completion
                continue
            state['defined'] += 1
            if state['numerator_range'] is None:
                state['numerator_range'] = [numerator, numerator]
                state['denominator_range'] = [denominator, denominator]
            else:
                state['numerator_range'][0] = min(state['numerator_range'][0], numerator)
                state['numerator_range'][1] = max(state['numerator_range'][1], numerator)
                state['denominator_range'][0] = min(state['denominator_range'][0], denominator)
                state['denominator_range'][1] = max(state['denominator_range'][1], denominator)
            conversion = measures(numerator, denominator, definition)['conversion_gap']
            state['meets'] += int(numerator * 10000 >= denominator * definition.target_basis_points)
            if state['ratio_min_key'] is None or _ratio_cmp(numerator, denominator, *state['ratio_min_key'][:2]) < 0:
                state['ratio_min_key'] = (numerator, denominator, completion)
            if state['ratio_max_key'] is None or _ratio_cmp(numerator, denominator, *state['ratio_max_key'][:2]) > 0:
                state['ratio_max_key'] = (numerator, denominator, completion)
            if state['conversion_min_key'] is None or (conversion, completion) < (state['conversion_min_key'][0], state['conversion_min_key'][1]):
                state['conversion_min_key'] = (conversion, completion)
            if state['conversion_max_key'] is None or (conversion, completion) > (state['conversion_max_key'][0], state['conversion_max_key'][1]):
                state['conversion_max_key'] = (conversion, completion)
        if has_zero:
            completions_with_zero += 1
    # Build only the witnesses actually referenced by an endpoint or zero state.
    witnesses = {}

    def witness_for(completion):
        if completion is None:
            return None
        value = build_witness(catalog, current, parsed, completion, contributions, base_region, facilities)
        witnesses.setdefault(value['completion_digest'], value)
        return value['completion_digest']

    for state in stats.values():
        state['zero_witness'] = witness_for(state.pop('zero_completion'))
        if state['ratio_min_key'] is not None:
            state['ratio_min'] = decimal_ratio(state['ratio_min_key'][0], state['ratio_min_key'][1])
            state['ratio_max'] = decimal_ratio(state['ratio_max_key'][0], state['ratio_max_key'][1])
            state['ratio_min_witness'] = witness_for(state['ratio_min_key'][2])
            state['ratio_max_witness'] = witness_for(state['ratio_max_key'][2])
            state['conversion_min'] = state['conversion_min_key'][0]
            state['conversion_max'] = state['conversion_max_key'][0]
            state['conversion_min_witness'] = witness_for(state['conversion_min_key'][1])
            state['conversion_max_witness'] = witness_for(state['conversion_max_key'][1])
    rows = []
    for region in sorted(catalog.region_map):
        relaxation = outer_bounds(parsed, contributions, base_region[region], region, definition.target_basis_points)
        rows.append(_region_report(catalog, region, base_region[region], stats[region], definition, truncated, relaxation))
    report = {
        **header,
        'status': 'resource_limit_exceeded' if truncated else 'analyzed',
        'analysis_performed': True,
        'conflicts': [],
        'feasible_completions': len(completions),
        'truncated': truncated,
        'nodes_evaluated': nodes,
        'base_confirmed_beds': len(current['beds']),
        'base_indicator': {region: base_region[region] for region in sorted(base_region)},
        'witnesses': {key: witnesses[key] for key in sorted(witnesses)},
        'regions': rows,
        'zero_denominator_completions': completions_with_zero,
        'license_expansion_assumed': False,
    }
    report['report_digest'] = fingerprint(report)
    return report


def _interval_width(low, high):
    if low is None or high is None:
        return None
    return str(Decimal(high) - Decimal(low))


def compare_narrowing(baseline, narrowed):
    """Compare a pinned follow-up report with its retained baseline report."""
    for value in (baseline, narrowed):
        if value.get('analysis') != 'disputed-evidence-interval':
            raise ValueError('narrowing comparison requires dispute interval reports')
    for key in ('day', 'known_at', 'worksheet_digest', 'definition_digest', 'snapshot_digest'):
        if baseline.get(key) != narrowed.get(key):
            raise ValueError('narrowing reports differ in ' + key)
    if baseline.get('pin'):
        raise ValueError('baseline report must not already be pinned')
    if not narrowed.get('pin'):
        raise ValueError('narrowed report must contain at least one pin')
    if baseline.get('status') != 'analyzed':
        raise ValueError('baseline report must be analyzable')
    if narrowed.get('status') == 'evidence_conflict':
        raise ValueError('narrowed report contains an evidence conflict')
    if narrowed.get('status') not in ('analyzed', 'resource_limit_exceeded'):
        raise ValueError('unknown narrowed report status')
    before_rows = {row['region']: row for row in baseline.get('regions', [])}
    after_rows = {row['region']: row for row in narrowed.get('regions', [])}
    rows = []
    narrowed_regions = 0
    for region in sorted(before_rows.keys() | after_rows.keys()):
        before, after = before_rows.get(region), after_rows.get(region)
        if before is None or after is None:
            rows.append({'region': region, 'state': 'unpaired'})
            continue
        before_width = _interval_width(before['ratio']['min'], before['ratio']['max'])
        after_width = _interval_width(after['ratio']['min'], after['ratio']['max'])
        before_conversion = None
        if before['conversion_needed']['min'] is not None and before['conversion_needed']['max'] is not None:
            before_conversion = before['conversion_needed']['max'] - before['conversion_needed']['min']
        after_conversion = None
        if after['conversion_needed']['min'] is not None and after['conversion_needed']['max'] is not None:
            after_conversion = after['conversion_needed']['max'] - after['conversion_needed']['min']
        ratio_reduction = None
        if before_width is not None and after_width is not None:
            ratio_reduction = str(Decimal(before_width) - Decimal(after_width))
            narrowed_regions += int(Decimal(after_width) < Decimal(before_width))
        conversion_reduction = None
        if before_conversion is not None and after_conversion is not None:
            conversion_reduction = before_conversion - after_conversion
        rows.append({
            'region': region,
            'state': 'paired',
            'baseline': {
                'ratio_min': before['ratio']['min'],
                'ratio_max': before['ratio']['max'],
                'ratio_width': before_width,
                'conversion_min': before['conversion_needed']['min'],
                'conversion_max': before['conversion_needed']['max'],
                'conversion_width': before_conversion,
                'conclusion': before['conclusion'],
                'zero_denominator_only': before['zero_denominator']['only_state'],
            },
            'narrowed': {
                'ratio_min': after['ratio']['min'],
                'ratio_max': after['ratio']['max'],
                'ratio_width': after_width,
                'conversion_min': after['conversion_needed']['min'],
                'conversion_max': after['conversion_needed']['max'],
                'conversion_width': after_conversion,
                'conclusion': after['conclusion'],
                'zero_denominator_only': after['zero_denominator']['only_state'],
            },
            'ratio_width_reduction': ratio_reduction,
            'conversion_width_reduction': conversion_reduction,
            'conclusion_changed': before['conclusion'] != after['conclusion'],
        })
    return {
        'analysis': 'disputed-evidence-narrowing',
        'version': 1,
        'snapshot_digest': baseline['snapshot_digest'],
        'worksheet_digest': baseline['worksheet_digest'],
        'definition_digest': baseline['definition_digest'],
        'pin': narrowed['pin'],
        'baseline_report_digest': baseline.get('report_digest'),
        'narrowed_report_digest': narrowed.get('report_digest'),
        'baseline_report_retained': True,
        'mutates_baseline': False,
        'narrowed_regions': narrowed_regions,
        'regions': rows,
    }
