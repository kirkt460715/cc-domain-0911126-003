import argparse
import json
from pathlib import Path
import sys
import tempfile
from registry.values import canonical, day_range
from registry.intake import preview, extract_delta, knowledge_inventory
from bed_events.schema import parse_package
from bed_events.replay import snapshot
from bed_events.ledger import Ledger
from bed_events.tabular import read_directory, write_directory
from bed_events.timeline import exposure, bed_timeline
from indicators.definitions import nursing_definition, parse_definition
from indicators.compute import compute, compare_indicators
from indicators.periods import period_indicator, daily_series, threshold_spells
from indicators.disclosure import protect_regions
from indicators.portfolio import definition_comparison, facility_contributions, conversion_plan, attribution_bridge
from audit.quality import quality_report, check_additivity, trace_coverage, revision_inventory
from audit.revisions import revision_impact, explain_bed, compare_snapshots
from audit.claims import reconcile_claims, verify_reported_ratios
from audit.package import write_package, verify_package
from audit.recovery import backup, restore
from audit.query import beds, facility_drilldown, partition_summary
from audit.jobs import SnapshotQueue
from indicators.registry import Definitions
from audit.provenance import lineage_for_bed
from audit.http import make_server
from bed_events.staging import Staging
from audit.review import Reviews
from indicators.flows import stock_flow_bridge, daily_capacity_utilization


def load(path):
    target = Path(path)
    if not target.is_file() or target.stat().st_size > 100_000_000:
        raise ValueError('input must be bounded local JSON file')
    return json.loads(target.read_text(encoding='utf-8'))


def report(document, day, known_at, definition=None):
    catalog, events = parse_package(document)
    definition = definition or nursing_definition()
    current = snapshot(catalog, events, day, known_at)
    indicator = compute(catalog, current, definition)
    return {
        'snapshot': current,
        'indicator': indicator,
        'public_indicator': protect_regions(catalog, indicator),
        'quality': quality_report(catalog, events, day, known_at),
        'additivity': check_additivity(catalog, indicator),
        'trace_coverage': trace_coverage(current),
        'partitions': partition_summary(current),
    }


def demo(fixture):
    document = load(fixture)
    catalog, events = parse_package(document)
    definition = nursing_definition()
    early = '2026-11-02T00:00:00Z'
    latest = '2026-11-10T00:00:00Z'
    day = '2026-11-01'
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with Ledger(root / 'registry.sqlite') as ledger:
            first = ledger.ingest('synthetic-import', document)
            if ledger.ingest('synthetic-import', document)['chain_hash'] != first['chain_hash']:
                raise AssertionError('duplicate import changed ledger')
            prior = ledger.create_snapshot('prior', day, early)
            current = ledger.create_snapshot('current', day, latest)
            published = ledger.publish('current-release', 'current')
            evidence = write_package(document, current['result'], definition, root / 'audit')
            verify_package(root / 'audit')
            write_directory(document, root / 'tables')
            if read_directory(root / 'tables') != document:
                raise AssertionError('CSV roundtrip changed event package')
            queue = SnapshotQueue(ledger)
            queue.enqueue('offline-audit', day, latest)
            job = queue.work('offline-audit')
            saved = backup(ledger, root / 'backup.sqlite')
            restored = restore(root / 'backup.sqlite', root / 'restored.sqlite', saved['digest'])
            indicator = compute(catalog, current['result'], definition)
            impact = revision_impact(catalog, events, [day], early, latest, definition)
            return {
                'synthetic': True,
                'snapshot_digest': current['result_digest'],
                'published_label': published['label'],
                'regional_rows': indicator['rows'],
                'revision_impact': impact['changed_bed_day_cells'],
                'evidence': evidence,
                'job_state': job['state'],
                'ledger': ledger.audit(),
                'restored': restored['audit'],
            }


def parser():
    result = argparse.ArgumentParser(prog='bed_audit')
    result.add_argument('--db', default='registry.sqlite')
    sub = result.add_subparsers(dest='command', required=True)
    command = sub.add_parser('demo')
    command.add_argument('--fixture', required=True)
    for name in ('report', 'quality', 'inventory', 'exposure', 'period', 'daily', 'revision-impact', 'explain', 'conversion', 'stock-bridge', 'capacity-series'):
        command = sub.add_parser(name)
        command.add_argument('--fixture', required=True)
        command.add_argument('--day', default='2026-11-01')
        command.add_argument('--known-at', default='2026-11-10T00:00:00Z')
        command.add_argument('--definition')
        if name in ('period', 'daily', 'exposure', 'stock-bridge', 'capacity-series'):
            command.add_argument('--start', required=True)
            command.add_argument('--end', required=True)
        if name == 'revision-impact':
            command.add_argument('--before-known', required=True)
        if name == 'explain':
            command.add_argument('--bed', required=True)
        if name == 'conversion':
            command.add_argument('--region', required=True)
            command.add_argument('--budgets', required=True)
    command = sub.add_parser('ingest')
    command.add_argument('--key', required=True)
    sources = command.add_mutually_exclusive_group(required=True)
    sources.add_argument('--fixture')
    sources.add_argument('--tables')
    command = sub.add_parser('preview')
    command.add_argument('--fixture', required=True)
    command = sub.add_parser('snapshot')
    command.add_argument('id')
    command.add_argument('--day', required=True)
    command.add_argument('--known-at', required=True)
    command = sub.add_parser('publish')
    command.add_argument('snapshot')
    command.add_argument('label')
    command = sub.add_parser('query')
    command.add_argument('snapshot')
    command.add_argument('--filters', default='{}')
    command.add_argument('--columns')
    command.add_argument('--cursor')
    command.add_argument('--limit', type=int, default=100)
    command = sub.add_parser('export')
    command.add_argument('snapshot')
    command.add_argument('directory')
    command = sub.add_parser('verify-package')
    command.add_argument('directory')
    command = sub.add_parser('backup')
    command.add_argument('destination')
    command = sub.add_parser('restore')
    command.add_argument('source')
    command.add_argument('destination')
    command.add_argument('digest')
    command = sub.add_parser('serve')
    command.add_argument('--host', default='127.0.0.1')
    command.add_argument('--port', type=int, default=8080)
    command = sub.add_parser('stage')
    command.add_argument('id')
    command.add_argument('--fixture', required=True)
    command.add_argument('--expected-revision', type=int)
    command = sub.add_parser('stage-inspect')
    command.add_argument('--selections', required=True)
    command = sub.add_parser('stage-promote')
    command.add_argument('--selections', required=True)
    command.add_argument('--key', required=True)
    command.add_argument('--expected-digest', required=True)
    command = sub.add_parser('review-open')
    command.add_argument('id')
    command.add_argument('snapshot')
    command = sub.add_parser('review-decide')
    command.add_argument('id')
    command.add_argument('--revision', type=int, required=True)
    command.add_argument('--reviewer', required=True)
    command.add_argument('--decision', choices=['approve','reject'], required=True)
    command.add_argument('--reason', required=True)
    command = sub.add_parser('review-public')
    command.add_argument('id')
    command = sub.add_parser('define')
    command.add_argument('--fixture', required=True)
    command = sub.add_parser('definitions')
    command.add_argument('--id')
    command = sub.add_parser('provenance')
    command.add_argument('--bed', required=True)
    command = sub.add_parser('facility-analysis')
    command.add_argument('snapshot')
    command.add_argument('--region', required=True)
    command = sub.add_parser('definition-comparison')
    command.add_argument('snapshot')
    command.add_argument('--definitions', required=True)
    command = sub.add_parser('attribution')
    command.add_argument('--before', required=True)
    command.add_argument('--after', required=True)
    sub.add_parser('audit')
    sub.add_parser('history')
    return result


def dispatch(args):
    command = args.command
    if command == 'demo':
        return demo(args.fixture)
    if command == 'verify-package':
        return verify_package(args.directory)
    if command == 'restore':
        return restore(args.source, args.destination, args.digest)
    if command == 'serve':
        server = make_server(args.db, args.host, args.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return {'stopped': True}
    if hasattr(args, 'fixture') and args.fixture and command not in ('ingest', 'preview', 'stage', 'define'):
        document = load(args.fixture)
        catalog, events = parse_package(document)
        definition = parse_definition(load(args.definition)) if args.definition else nursing_definition()
        if command == 'report':
            return report(document, args.day, args.known_at, definition)
        if command == 'quality':
            return quality_report(catalog, events, args.day, args.known_at)
        if command == 'inventory':
            return {'revisions': revision_inventory(catalog, events), 'knowledge': knowledge_inventory(catalog, events, args.known_at)}
        if command == 'stock-bridge':
            return stock_flow_bridge(catalog, events, args.start, args.end, args.known_at, definition)
        if command == 'capacity-series':
            return daily_capacity_utilization(catalog, events, args.start, args.end, args.known_at)
        if command == 'period':
            return period_indicator(catalog, events, args.start, args.end, args.known_at, definition)
        if command == 'daily':
            series = daily_series(catalog, events, args.start, args.end, args.known_at, definition)
            return {'series': series, 'below_target_spells': threshold_spells(series, 'region')}
        if command == 'exposure':
            return exposure(catalog, events, args.start, args.end, args.known_at, definition)
        if command == 'revision-impact':
            return revision_impact(catalog, events, [args.day], args.before_known, args.known_at, definition)
        if command == 'explain':
            return explain_bed(catalog, events, args.bed, args.day, args.known_at)
        if command == 'conversion':
            current = snapshot(catalog, events, args.day, args.known_at)
            return conversion_plan(catalog, current, definition, args.region, json.loads(args.budgets))
    with Ledger(args.db) as ledger:
        if command == 'stage':
            return Staging(ledger).put(args.id, load(args.fixture), args.expected_revision)
        if command == 'stage-inspect':
            return Staging(ledger).inspect(json.loads(args.selections))
        if command == 'stage-promote':
            return Staging(ledger).promote(json.loads(args.selections), args.key, args.expected_digest)
        if command == 'review-open':
            return Reviews(ledger).open(args.id, args.snapshot, nursing_definition().export())
        if command == 'review-decide':
            return Reviews(ledger).decide(args.id, args.revision, args.reviewer, args.decision == 'approve', args.reason)
        if command == 'review-public':
            return Reviews(ledger).approved_public_report(args.id)
        if command == 'define':
            return Definitions(ledger).register(load(args.fixture))
        if command == 'definitions':
            return Definitions(ledger).list(args.id)
        if command == 'provenance':
            return lineage_for_bed(ledger, args.bed)
        if command == 'facility-analysis':
            stored = ledger.get_snapshot(args.snapshot)
            catalog, events = parse_package(stored['document'])
            return facility_contributions(catalog, stored['result'], nursing_definition(), args.region)
        if command == 'definition-comparison':
            stored = ledger.get_snapshot(args.snapshot)
            catalog, events = parse_package(stored['document'])
            return definition_comparison(catalog, stored['result'], load(args.definitions))
        if command == 'attribution':
            before = ledger.get_snapshot(args.before)
            after = ledger.get_snapshot(args.after)
            catalog, events = parse_package(after['document'])
            return attribution_bridge(catalog, before['result'], after['result'], nursing_definition())
        if command == 'ingest':
            document = load(args.fixture) if args.fixture else read_directory(args.tables)
            return ledger.ingest(args.key, document)
        if command == 'preview':
            return preview(ledger, load(args.fixture))
        if command == 'snapshot':
            return ledger.create_snapshot(args.id, args.day, args.known_at)
        if command == 'publish':
            return ledger.publish(args.label, args.snapshot)
        if command == 'query':
            current = ledger.get_snapshot(args.snapshot)['result']
            columns = args.columns.split(',') if args.columns else None
            return beds(current, json.loads(args.filters), columns, args.limit, args.cursor)
        if command == 'export':
            current = ledger.get_snapshot(args.snapshot)
            return write_package(current['document'], current['result'], nursing_definition(), args.directory)
        if command == 'backup':
            return backup(ledger, args.destination)
        if command == 'audit':
            return ledger.audit()
        if command == 'history':
            return ledger.history()
    raise ValueError('unknown command')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
    except (ValueError, KeyError, OSError) as exc:
        print(canonical({'error': type(exc).__name__, 'message': str(exc)}), file=sys.stderr)
        return 2
    print(canonical(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
