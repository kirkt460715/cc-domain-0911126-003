import copy
from bed_events.replay import verify_snapshot
from registry.values import fingerprint


COUNT_FIELDS = {'numerator', 'denominator', 'ratio', 'ratio_basis_points_floor', 'meets_target', 'conversion_gap', 'additional_nursing_gap', 'facility_count_with_beds'}


def protect_regions(catalog, indicator, threshold=None):
    if indicator['level'] != 'region':
        raise ValueError('hierarchical protection requires regional indicator')
    if threshold is None:
        threshold = indicator['definition']['small_denominator']
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise ValueError('invalid disclosure threshold')
    rows = {row['region']: copy.deepcopy(row) for row in indicator['rows']}
    protected = {ident for ident, row in rows.items() if 0 < row['denominator'] < threshold}
    primary = set(protected)
    for ident in list(primary):
        protected.update(catalog.ancestors(ident))
    result = []
    for ident, row in sorted(rows.items()):
        if ident in protected:
            reason = 'small_denominator' if ident in primary else 'descendant_complement_protection'
            row = {key: value for key, value in row.items() if key not in COUNT_FIELDS}
            for key in COUNT_FIELDS:
                row[key] = None
            row['suppressed'] = True
            row['suppression_reason'] = reason
        else:
            row['suppressed'] = False
            row['suppression_reason'] = None
        result.append(row)
    return {
        'day': indicator['day'],
        'known_at': indicator['known_at'],
        'snapshot_digest': indicator['snapshot_digest'],
        'definition_digest': indicator['definition_digest'],
        'level': 'region',
        'threshold': threshold,
        'rows': result,
        'quality': copy.deepcopy(indicator['quality']),
        'disclosure_policy': 'suppress_small_cells_and_all_ancestors',
    }


def validate_protection(catalog, report):
    rows = {row['region']: row for row in report['rows']}
    if len(rows) != len(report['rows']):
        raise ValueError('duplicate region in disclosure report')
    for ident, row in rows.items():
        if row['suppressed']:
            if any(row.get(key) is not None for key in COUNT_FIELDS):
                raise ValueError('suppressed cell retains a sensitive measure')
            for ancestor in catalog.ancestors(ident):
                if ancestor in rows and not rows[ancestor]['suppressed']:
                    raise ValueError('ancestor permits complementary disclosure')
        elif row['denominator'] and row['denominator'] < report['threshold']:
            raise ValueError('small denominator was not suppressed')
    return True


def public_snapshot_summary(snapshot):
    verify_snapshot(snapshot)
    return {
        'day': snapshot['day'],
        'known_at': snapshot['known_at'],
        'snapshot_digest': snapshot['snapshot_digest'],
        'publishable': snapshot['publishable'],
        'quality': {
            'quarantined': bool(snapshot['quarantined']),
            'license_violations': bool(snapshot['license_violations']),
            'excluded': bool(snapshot['excluded']),
        },
    }
