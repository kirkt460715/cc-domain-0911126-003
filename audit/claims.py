from collections import defaultdict
from decimal import Decimal, InvalidOperation
from registry.values import object_fields, identifier, integer, decimal_ratio, fingerprint
from registry.catalog import CATEGORIES
from indicators.compute import compute


def parse_claims(rows):
    if not isinstance(rows, list) or len(rows) > 100000:
        raise ValueError('claims must be bounded list')
    seen = set()
    result = []
    for index, row in enumerate(rows):
        object_fields(row, ('facility', 'category', 'count'), path=f'claims[{index}]')
        identifier(row['facility'], 'claim.facility')
        if row['category'] not in CATEGORIES:
            raise ValueError('unknown claim category')
        integer(row['count'], 'claim.count')
        key = row['facility'], row['category']
        if key in seen:
            raise ValueError('duplicate facility category claim')
        seen.add(key)
        result.append(dict(row))
    return result


def reconcile_claims(catalog, snapshot, claims):
    claims = parse_claims(claims)
    actual = defaultdict(int)
    expected = {}
    for bed in snapshot['beds']:
        actual[(bed['facility'], bed['category'])] += 1
    for row in claims:
        expected[(row['facility'], row['category'])] = row['count']
    findings = []
    for key in sorted(actual.keys() | expected.keys()):
        observed = actual[key]
        declared = expected.get(key)
        known_facility = key[0] in catalog.facility_versions
        if not known_facility:
            state = 'unknown_facility'
        elif declared is None:
            state = 'missing_claim'
        elif declared != observed:
            state = 'count_mismatch'
        else:
            state = 'matched'
        findings.append({
            'facility': key[0],
            'category': key[1],
            'declared': declared,
            'observed': observed,
            'difference': None if declared is None else declared - observed,
            'state': state,
        })
    disputed = [row for row in findings if row['state'] != 'matched']
    return {
        'snapshot_digest': snapshot['snapshot_digest'],
        'claims_digest': fingerprint(claims),
        'rows': findings,
        'matched_cells': len(findings) - len(disputed),
        'disputed_cells': len(disputed),
        'sum_absolute_difference': sum(abs(row['difference']) for row in findings if row['difference'] is not None),
        'snapshot_publishable': snapshot['publishable'],
        'fully_reconciled': not disputed and snapshot['publishable'],
    }


def verify_reported_ratios(indicator, declared):
    if not isinstance(declared, list):
        raise ValueError('reported ratios must be list')
    level = indicator['level']
    actual = {row[level]: row for row in indicator['rows']}
    findings = []
    seen = set()
    for item in declared:
        object_fields(item, (level, 'numerator', 'denominator', 'ratio'))
        ident = item[level]
        if ident in seen:
            raise ValueError('duplicate reported indicator')
        seen.add(ident)
        integer(item['numerator'], 'reported.numerator')
        integer(item['denominator'], 'reported.denominator')
        reasons = []
        expected = actual.get(ident)
        if expected is None:
            reasons.append('unknown_group')
        else:
            for key in ('numerator', 'denominator'):
                if item[key] != expected[key]:
                    reasons.append(key + '_mismatch')
        if item['denominator'] == 0:
            if item['ratio'] is not None:
                reasons.append('zero_denominator_requires_null_ratio')
        else:
            try:
                ratio = Decimal(str(item['ratio']))
                if not ratio.is_finite() or not Decimal(0) <= ratio <= Decimal(1):
                    raise InvalidOperation
                expected_ratio = Decimal(item['numerator']) / Decimal(item['denominator'])
                if abs(ratio - expected_ratio) > Decimal('0.000000000001'):
                    reasons.append('ratio_arithmetic_mismatch')
            except (InvalidOperation, ValueError):
                reasons.append('invalid_ratio')
        findings.append({level: ident, 'valid': not reasons, 'reasons': reasons})
    for ident in sorted(actual.keys() - seen):
        findings.append({level: ident, 'valid': False, 'reasons': ['missing_reported_group']})
    return {'definition_digest': indicator['definition_digest'], 'rows': findings, 'valid': all(row['valid'] for row in findings)}
