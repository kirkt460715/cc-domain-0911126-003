from collections import defaultdict
from registry.values import day_range, next_day, decimal_ratio, iso_day, stamp
from bed_events.replay import snapshot
from indicators.compute import compute


def change_boundaries(catalog, events, start, end, known_at):
    days = day_range(start, end)
    stamp(known_at, 'known_at')
    boundaries = {start, end}
    for row in events.visible(known_at):
        day = row['effective_at'][:10]
        if start < day < end:
            boundaries.add(day)
    for ident in catalog.facility_versions:
        facility = catalog.visible_facility(ident, known_at)
        if facility is None:
            continue
        for key in ('open', 'close'):
            if start < facility[key] < end:
                boundaries.add(facility[key])
    return sorted(boundaries)


def period_indicator(catalog, events, start, end, known_at, definition, level='region'):
    days = day_range(start, end)
    boundaries = change_boundaries(catalog, events, start, end, known_at)
    groups = {}
    segments = []
    unknown_days = 0
    for left, right in zip(boundaries, boundaries[1:]):
        span_days = len(day_range(left, right))
        current = snapshot(catalog, events, left, known_at)
        indicator = compute(catalog, current, definition, level)
        if not current['publishable']:
            unknown_days += span_days
        for row in indicator['rows']:
            key = row[level]
            group = groups.setdefault(key, {'numerator_bed_days': 0, 'denominator_bed_days': 0, 'days_with_denominator': 0, 'days_meeting_target': 0, 'days_below_target': 0, 'observed_days': 0})
            group['numerator_bed_days'] += row['numerator'] * span_days
            group['denominator_bed_days'] += row['denominator'] * span_days
            group['observed_days'] += span_days
            if row['denominator']:
                group['days_with_denominator'] += span_days
                if row['meets_target']:
                    group['days_meeting_target'] += span_days
                else:
                    group['days_below_target'] += span_days
        segments.append({'start': left, 'end': right, 'days': span_days, 'snapshot_digest': current['snapshot_digest'], 'publishable': current['publishable']})
    rows = []
    for key, group in sorted(groups.items()):
        rows.append({
            level: key,
            **group,
            'period_ratio': decimal_ratio(group['numerator_bed_days'], group['denominator_bed_days']),
            'mean_daily_denominator': decimal_ratio(group['denominator_bed_days'], len(days)),
            'mean_daily_numerator': decimal_ratio(group['numerator_bed_days'], len(days)),
        })
    return {
        'start_inclusive': start,
        'end_exclusive': end,
        'known_at': known_at,
        'days': len(days),
        'definition_digest': definition.digest,
        'level': level,
        'rows': rows,
        'segments': segments,
        'quality_days': unknown_days,
        'publishable': unknown_days == 0,
        'time_measure': 'end_of_day_stock_weighted_by_calendar_days',
    }


def daily_series(catalog, events, start, end, known_at, definition, level='region'):
    result = []
    for day in day_range(start, end):
        current = snapshot(catalog, events, day, known_at)
        indicator = compute(catalog, current, definition, level)
        for row in indicator['rows']:
            result.append({'day': day, **row, 'publishable': current['publishable']})
    return {'definition_digest': definition.digest, 'level': level, 'rows': result}


def threshold_spells(series, level):
    groups = defaultdict(list)
    for row in series['rows']:
        groups[row[level]].append(row)
    result = []
    for key, rows in sorted(groups.items()):
        rows.sort(key=lambda row: row['day'])
        spell = None
        for row in rows:
            below = row['meets_target'] is False and row['publishable']
            consecutive = spell is not None and next_day(spell['last_day']) == row['day']
            if below and consecutive:
                spell['last_day'] = row['day']
                spell['days'] += 1
                spell['maximum_conversion_gap'] = max(spell['maximum_conversion_gap'], row['conversion_gap'])
            else:
                if spell is not None:
                    result.append(spell)
                spell = None
                if below:
                    spell = {level: key, 'first_day': row['day'], 'last_day': row['day'], 'days': 1, 'maximum_conversion_gap': row['conversion_gap']}
        if spell is not None:
            result.append(spell)
    return result
