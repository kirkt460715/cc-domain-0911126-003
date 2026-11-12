from registry.values import object_fields, identifier, canonical
from bed_events.ledger import Ledger
from bed_events.schema import parse_package
from bed_events.replay import snapshot
from indicators.definitions import nursing_definition, parse_definition
from indicators.compute import compute
from indicators.disclosure import protect_regions
from indicators.portfolio import facility_contributions, conversion_plan
from audit.query import beds, facility_drilldown
from audit.quality import quality_report, revision_inventory
from audit.revisions import explain_bed, revision_impact
from audit.claims import reconcile_claims
from audit.jobs import SnapshotQueue
from bed_events.staging import Staging
from audit.review import Reviews
from audit.triage import Triage
from audit.provenance import lineage_for_bed
from indicators.registry import Definitions
from bed_events.timeline import exposure, bed_timeline
from indicators.periods import period_indicator, daily_series, threshold_spells
from indicators.portfolio import definition_comparison, attribution_bridge
from indicators.flows import stock_flow_bridge, daily_capacity_utilization


class Service:
    def __init__(self, path):
        self.path = path

    def handle(self, method, path, data):
        if not isinstance(data, dict):
            raise ValueError('request must be JSON object')
        if method == 'GET' and path == '/health':
            if data:
                raise ValueError('health endpoint takes no body')
            with Ledger(self.path) as ledger:
                state = ledger.audit(replay=False)
            return {'ok': state['valid'], 'head_hash': state['head_hash']}
        if method != 'POST':
            raise LookupError('unknown endpoint')
        with Ledger(self.path) as ledger:
            if path == '/ingest':
                object_fields(data, ('request_key', 'document'))
                return ledger.ingest(data['request_key'], data['document'])
            if path == '/stages/put':
                object_fields(data, ('id', 'document'), ('expected_revision',))
                return Staging(ledger).put(data['id'], data['document'], data.get('expected_revision'))
            if path == '/stages/inspect':
                object_fields(data, ('selections',))
                return Staging(ledger).inspect(data['selections'])
            if path == '/stages/promote':
                object_fields(data, ('selections', 'request_key', 'expected_digest'))
                return Staging(ledger).promote(data['selections'], data['request_key'], data['expected_digest'])
            if path == '/stages/pending':
                object_fields(data, ())
                return {'stages': Staging(ledger).pending()}
            if path == '/reviews/open':
                object_fields(data, ('id', 'snapshot'), ('definition',))
                definition = data.get('definition', nursing_definition().export())
                return Reviews(ledger).open(data['id'], data['snapshot'], definition)
            if path == '/reviews/decide':
                object_fields(data, ('id', 'expected_revision', 'reviewer', 'approve', 'reason'))
                return Reviews(ledger).decide(data['id'], data['expected_revision'], data['reviewer'], data['approve'], data['reason'])
            if path == '/reviews/public':
                object_fields(data, ('id',))
                return Reviews(ledger).approved_public_report(data['id'])
            if path == '/definitions/register':
                object_fields(data, ('definition',))
                return Definitions(ledger).register(data['definition'])
            if path == '/definitions/list':
                object_fields(data, (), ('id',))
                return {'definitions': Definitions(ledger).list(data.get('id'))}
            if path == '/definitions/changes':
                object_fields(data, ('id', 'before', 'after'))
                return Definitions(ledger).changes(data['id'], data['before'], data['after'])
            if path == '/triage/open':
                object_fields(data, ('id', 'snapshot', 'issue_key'))
                return Triage(ledger).open(data['id'], data['snapshot'], data['issue_key'])
            if path == '/triage/assign':
                object_fields(data, ('id', 'revision', 'assignee'))
                return Triage(ledger).assign(data['id'], data['revision'], data['assignee'])
            if path == '/triage/resolve':
                object_fields(data, ('id', 'revision', 'resolution_snapshot', 'reason'))
                return Triage(ledger).resolve(data['id'], data['revision'], data['resolution_snapshot'], data['reason'])
            if path == '/triage/get':
                object_fields(data, ('id',))
                return Triage(ledger).get(data['id'])
            if path == '/provenance/bed':
                object_fields(data, ('bed',))
                return lineage_for_bed(ledger, data['bed'])
            if path == '/snapshot':
                object_fields(data, ('id', 'day', 'known_at'))
                return ledger.create_snapshot(data['id'], data['day'], data['known_at'])
            if path == '/publish':
                object_fields(data, ('label', 'snapshot'))
                return ledger.publish(data['label'], data['snapshot'])
            if path == '/publication':
                object_fields(data, ('label',))
                return ledger.publication(data['label'])
            if path == '/history':
                object_fields(data, (), ('after', 'limit'))
                return {'batches': ledger.history(data.get('after', 0), data.get('limit', 100))}
            if path == '/audit':
                object_fields(data, ())
                return ledger.audit()
            if path == '/jobs/enqueue':
                object_fields(data, ('id', 'day', 'known_at'))
                return SnapshotQueue(ledger).enqueue(data['id'], data['day'], data['known_at'])
            if path == '/jobs/work':
                object_fields(data, ('id',))
                return SnapshotQueue(ledger).work(data['id'])
            if path == '/jobs/get':
                object_fields(data, ('id',))
                return SnapshotQueue(ledger).get(data['id'])
            if path == '/jobs/retry':
                object_fields(data, ('id',))
                return SnapshotQueue(ledger).retry(data['id'])
            if path in ('/period', '/daily', '/exposure', '/timeline', '/stock-bridge', '/capacity-series'):
                required = ('start', 'end', 'known_at', 'bed') if path == '/timeline' else ('start', 'end', 'known_at')
                object_fields(data, required, ('definition', 'level'))
                catalog, events = parse_package(ledger.document())
                definition = parse_definition(data['definition']) if 'definition' in data else nursing_definition()
                level = data.get('level', 'region')
                if path == '/period':
                    return period_indicator(catalog, events, data['start'], data['end'], data['known_at'], definition, level)
                if path == '/daily':
                    result = daily_series(catalog, events, data['start'], data['end'], data['known_at'], definition, level)
                    return {'series': result, 'spells': threshold_spells(result, level)}
                if path == '/exposure':
                    return exposure(catalog, events, data['start'], data['end'], data['known_at'], definition)
                if path == '/timeline':
                    return bed_timeline(catalog, events, data['bed'], data['start'], data['end'], data['known_at'])
                if path == '/stock-bridge':
                    return stock_flow_bridge(catalog, events, data['start'], data['end'], data['known_at'], definition)
                return daily_capacity_utilization(catalog, events, data['start'], data['end'], data['known_at'])
            if path == '/definition-comparison':
                object_fields(data, ('snapshot', 'definitions'), ('level',))
                stored = ledger.get_snapshot(data['snapshot'])
                catalog, events = parse_package(stored['document'])
                return definition_comparison(catalog, stored['result'], data['definitions'], data.get('level', 'region'))
            if path == '/attribution':
                object_fields(data, ('before_snapshot', 'after_snapshot'), ('definition',))
                before = ledger.get_snapshot(data['before_snapshot'])
                after = ledger.get_snapshot(data['after_snapshot'])
                catalog, events = parse_package(after['document'])
                definition = parse_definition(data['definition']) if 'definition' in data else nursing_definition()
                return attribution_bridge(catalog, before['result'], after['result'], definition)
            allowed = {'/indicator', '/beds', '/quality', '/explain', '/claims', '/facility', '/contributions', '/conversion', '/inventory', '/revision-impact'}
            if path not in allowed:
                raise LookupError('unknown endpoint')
            document = ledger.document()
            catalog, events = parse_package(document)
            if path == '/inventory':
                object_fields(data, ())
                return revision_inventory(catalog, events)
            if path == '/revision-impact':
                object_fields(data, ('days', 'before_known', 'after_known'), ('definition',))
                definition = parse_definition(data['definition']) if 'definition' in data else nursing_definition()
                return revision_impact(catalog, events, data['days'], data['before_known'], data['after_known'], definition)
            if path == '/explain':
                object_fields(data, ('bed', 'day', 'known_at'))
                return explain_bed(catalog, events, data['bed'], data['day'], data['known_at'])
            if path == '/quality':
                object_fields(data, ('day', 'known_at'))
                return quality_report(catalog, events, data['day'], data['known_at'])
            current_id = data.get('snapshot')
            if current_id is None:
                raise ValueError('analysis request requires immutable snapshot id')
            stored = ledger.get_snapshot(current_id)
            current = stored['result']
            catalog, events = parse_package(stored['document'])
            if path == '/beds':
                object_fields(data, ('snapshot',), ('filters', 'columns', 'limit', 'cursor'))
                return beds(current, data.get('filters'), data.get('columns'), data.get('limit', 100), data.get('cursor'))
            if path == '/facility':
                object_fields(data, ('snapshot', 'facility'))
                return facility_drilldown(catalog, current, data['facility'])
            if path == '/claims':
                object_fields(data, ('snapshot', 'claims'))
                return reconcile_claims(catalog, current, data['claims'])
            definition = parse_definition(data['definition']) if 'definition' in data else nursing_definition()
            if path == '/indicator':
                object_fields(data, ('snapshot',), ('definition', 'level', 'protect'))
                level = data.get('level', 'region')
                indicator = compute(catalog, current, definition, level)
                protect = data.get('protect', False)
                if not isinstance(protect, bool):
                    raise ValueError('protect must be boolean')
                return protect_regions(catalog, indicator) if protect else indicator
            if path == '/contributions':
                object_fields(data, ('snapshot', 'region'), ('definition',))
                return facility_contributions(catalog, current, definition, data['region'])
            if path == '/conversion':
                object_fields(data, ('snapshot', 'region', 'budgets'), ('definition',))
                return conversion_plan(catalog, current, definition, data['region'], data['budgets'])
        raise LookupError('unknown endpoint')
