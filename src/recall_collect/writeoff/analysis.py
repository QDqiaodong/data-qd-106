"""Offline write-off interval analysis over an accepted impact mart.

The task, in one paragraph: a returns CSV lists return events (unique
return id, customer id, a positive integer piece count and a JSON array of
existing candidate shipment ids of the *same* customer).  Every return must
be split integrally across its candidate shipments; every shipment may
receive at most its original ``units`` in total.  Candidate shipments may
mix recall-affected and merely ``not_linked_by_supplied_data`` shipments, so
all returns share shipment capacities and rows constrain one another: a
per-row greedy split is not valid.

Across **all** feasible allocations the analysis reports the exact minimum
and maximum of:

* ``affected_union`` - pieces allocated to the union of shipments that the
  mart links to at least one recall (each shipment counted once, so
  overlapping recalls are never added together);
* ``recall:<recall_id>`` - pieces allocated to the shipments that this one
  recall affects.

Every endpoint carries a complete integral allocation witness.  Metrics are
optimised independently; their extrema need not share one allocation.

When no feasible allocation exists the result is an infeasibility
certificate: a subset of returns whose candidates' total capacity is
strictly smaller than the subset's demanded pieces.  This is never reported
as a zero return.

Everything is exact integer arithmetic.  The solver runs one max-flow per
metric endpoint plus one bounded circulation to materialise the witness; it
never enumerates allocation schemes and never expands a piece count into
unit-sized steps.
"""
import json

from .errors import WriteoffInputError
from .flow import Dinic
from .mart_reader import load_mart_snapshot
from .returns_csv import read_returns_csv

REPORT_FORMAT = 'recall-writeoff-interval/1'


class WriteoffResult(dict):
    """A plain ``dict`` report; kept as a named type for documentation."""


def analyze_writeoff_intervals(mart_path, returns_csv_path):
    """Run the interval analysis and return a JSON-ready report dict.

    ``mart_path`` is an existing ``recall-impact-mart/1`` SQLite file, opened
    read-only and never modified.  ``returns_csv_path`` is the returns CSV
    described in :mod:`recall_collect.writeoff.returns_csv`.

    Raises :class:`WriteoffInputError` for any invalid input; a valid run
    with no feasible allocation returns a report whose ``status`` is
    ``'infeasible'`` and whose ``infeasibility`` field is the certificate.
    """
    mart = load_mart_snapshot(mart_path)
    returns_bundle = read_returns_csv(returns_csv_path)
    model = _build_model(mart, returns_bundle)
    return _analyze(model, mart, returns_bundle)


def _build_model(mart, returns_bundle):
    """Validate cross-file rules and freeze deterministic index order."""
    shipments = mart['shipments']
    records = sorted(returns_bundle['records'],
                     key=lambda row: row['return_id'])

    candidate_ids = []
    candidates_seen = set()
    for record in records:
        customer = record['customer_id']
        for shipment_id in record['candidates']:
            if shipment_id not in shipments:
                raise WriteoffInputError(
                    'returns CSV data record %d candidate shipment %r does '
                    'not exist in the impact mart'
                    % (record['position'], shipment_id))
            if shipments[shipment_id]['customer_id'] != customer:
                raise WriteoffInputError(
                    'returns CSV data record %d candidate shipment %r '
                    'belongs to customer %r, not %r'
                    % (record['position'], shipment_id,
                       shipments[shipment_id]['customer_id'], customer))
            if shipment_id not in candidates_seen:
                candidates_seen.add(shipment_id)
                candidate_ids.append(shipment_id)
    candidate_ids.sort()

    shipment_index = {sid: index for index, sid in enumerate(candidate_ids)}
    capacities = [shipments[sid]['units'] for sid in candidate_ids]

    return_rows = []
    for record in records:
        # Candidate order is a set semantically; sorting makes the report
        # independent of JSON-array ordering in the source CSV.
        indices = sorted(shipment_index[sid]
                         for sid in record['candidates'])
        return_rows.append({
            'return_id': record['return_id'],
            'customer_id': record['customer_id'],
            'units': record['units'],
            'position': record['position'],
            'candidates': indices,
        })

    affected = {
        recall_id: {shipment_index[sid]
                    for sid in sid_set if sid in shipment_index}
        for recall_id, sid_set in mart['recall_shipments'].items()
    }
    union_indices = set()
    for indices in affected.values():
        union_indices |= indices

    return {
        'returns': return_rows,
        'candidate_ids': candidate_ids,
        'capacities': capacities,
        'affected_by_recall': affected,
        'affected_union': union_indices,
    }


def _network_sizes(model):
    return len(model['returns']), len(model['candidate_ids'])


def _restricted_maxflow(model, allowed_shipments):
    """Largest number of pieces routable using only ``allowed`` shipments."""
    return_count, shipment_count = _network_sizes(model)
    source = return_count + shipment_count
    sink = source + 1
    network = Dinic(sink + 1)
    total_demand = 0
    for index, row in enumerate(model['returns']):
        network.add_edge(source, index, row['units'])
        total_demand += row['units']
    bound = total_demand + 1
    for index, row in enumerate(model['returns']):
        for shipment in row['candidates']:
            network.add_edge(index, return_count + shipment, bound)
    for shipment in allowed_shipments:
        network.add_edge(return_count + shipment, sink,
                         model['capacities'][shipment])
    return network.max_flow(source, sink)


def _feasibility(model):
    """Whether every return can be fully split within the shipment caps."""
    total_demand = sum(row['units'] for row in model['returns'])
    routed = _restricted_maxflow(
        model, range(len(model['candidate_ids'])))
    return routed == total_demand


def _infeasibility_certificate(model):
    """Extract a Hall-violating return subset from a failed max flow.

    The residual reachable set after a maximum flow of value ``v < F``
    contains the source and a set ``A`` of returns; every candidate shipment
    of ``A`` is also reachable (candidate edges carry capacity ``F + 1`` and
    can never be saturated).  The source side of that cut therefore has
    capacity ``F - demand(A) + capacity(N(A)) < F``, i.e.
    ``demand(A) > capacity(N(A))``.
    """
    returns = model['returns']
    capacities = model['capacities']
    return_count = len(returns)
    shipment_count = len(capacities)
    source = return_count + shipment_count
    sink = source + 1
    total_demand = sum(row['units'] for row in returns)
    network = Dinic(sink + 1)
    for index, row in enumerate(returns):
        network.add_edge(source, index, row['units'])
    bound = total_demand + 1
    for index, row in enumerate(returns):
        for shipment in row['candidates']:
            network.add_edge(index, return_count + shipment, bound)
    for shipment in range(shipment_count):
        network.add_edge(return_count + shipment, sink, capacities[shipment])
    network.max_flow(source, sink)
    reachable = network.residual_reachable(source)

    subset = [index for index in range(return_count) if index in reachable]
    candidate_set = set()
    for index in subset:
        candidate_set.update(returns[index]['candidates'])
    # Every candidate of a reachable return must itself be reachable; this
    # keeps the certificate closed under the candidate relation.  Candidate
    # arcs carry capacity F + 1 and can never saturate, so their forward
    # residual always survives a flow of value at most F.
    for index in subset:
        for shipment in returns[index]['candidates']:
            if return_count + shipment not in reachable:
                raise ValueError(
                    'internal error: infeasibility certificate is not '
                    'closed under the candidate relation')
    demanded = sum(returns[index]['units'] for index in subset)
    available = sum(capacities[shipment] for shipment in candidate_set)
    if not subset or demanded <= available:
        raise ValueError(
            'internal error: failed to extract a valid infeasibility '
            'certificate')
    certificate = {
        'returns': [
            {'return_id': returns[index]['return_id'],
             'units': returns[index]['units']}
            for index in subset],
        'candidate_shipments': [
            {'shipment_id': model['candidate_ids'][shipment],
             'units': capacities[shipment]}
            for shipment in sorted(candidate_set)],
        'demanded_units': demanded,
        'available_units': available,
        'shortfall_units': demanded - available,
    }
    return certificate


def _witness(model, target_shipments, target_units):
    """Materialise one complete allocation with exactly ``target_units`` on
    ``target_shipments``.

    A circulation with lower bounds is solved on the bipartite network:
    return demand edges force every return to be fully split, shipment
    capacities cap each shipment, and an exact arc aggregates the flow into
    the metric's target set.  The allocation is read back from the
    return -> shipment arcs.  One bounded circulation is solved; no scheme
    is enumerated.
    """
    returns = model['returns']
    capacities = model['capacities']
    return_count = len(returns)
    shipment_count = len(capacities)
    total_demand = sum(row['units'] for row in returns)

    target = sorted(target_shipments)
    target_units = int(target_units)
    if target_units < 0:
        raise ValueError('internal error: negative target units')

    source = return_count + shipment_count
    sink = source + 1
    aggregate = sink + 1 if target else None
    node_count = (aggregate + 1) if target else (sink + 1)

    edges = []

    def add_lower(u, v, lower, upper, kind, key=None):
        edges.append({'u': u, 'v': v, 'lower': lower, 'upper': upper,
                      'kind': kind, 'key': key})

    for index, row in enumerate(returns):
        add_lower(source, index, 0, row['units'], 'demand')
    for index, row in enumerate(returns):
        for shipment in row['candidates']:
            add_lower(index, return_count + shipment, 0, row['units'],
                      'assign', (index, shipment))
    target_set = set(target)
    for shipment in range(shipment_count):
        if aggregate is not None and shipment in target_set:
            add_lower(return_count + shipment, aggregate, 0,
                      capacities[shipment], 'shipment')
        else:
            add_lower(return_count + shipment, sink, 0,
                      capacities[shipment], 'shipment')
    if aggregate is not None:
        add_lower(aggregate, sink, target_units, target_units, 'metric')
    # Force the aggregate arc / every demand edge to carry exactly its
    # value by balancing the flow back, making the whole thing a circulation.
    add_lower(sink, source, total_demand, total_demand, 'return')

    balance = [0] * node_count
    for edge in edges:
        balance[edge['v']] += edge['lower']
        balance[edge['u']] -= edge['lower']

    super_source = node_count
    super_sink = node_count + 1
    network = Dinic(node_count + 2)
    handles = []
    for edge in edges:
        handles.append(network.add_edge(
            edge['u'], edge['v'], edge['upper'] - edge['lower']))
    required = 0
    for node, value in enumerate(balance):
        if value > 0:
            network.add_edge(super_source, node, value)
            required += value
        elif value < 0:
            network.add_edge(node, super_sink, -value)
    supplied = network.max_flow(super_source, super_sink)
    if supplied != required:
        raise ValueError(
            'internal error: requested witness is not realisable')

    allocation = {}
    for edge, handle in zip(edges, handles):
        if edge['kind'] != 'assign':
            continue
        amount = edge['lower'] + network.edge_flow(handle)
        if amount:
            allocation[edge['key']] = amount

    rows = []
    for index, row in enumerate(returns):
        assignments = []
        assigned = 0
        for shipment in row['candidates']:
            amount = allocation.get((index, shipment), 0)
            if not amount:
                continue
            requested = model['candidate_ids'][shipment]
            assignments.append({'shipment_id': requested, 'units': amount})
            assigned += amount
        if assigned != row['units']:
            raise ValueError(
                'internal error: witness does not conserve return demand')
        assignments.sort(key=lambda item: item['shipment_id'])
        rows.append({
            'return_id': row['return_id'],
            'units': row['units'],
            'assignments': assignments,
        })

    shipment_totals = {}
    for (index, shipment), amount in allocation.items():
        shipment_id = model['candidate_ids'][shipment]
        shipment_totals[shipment_id] = shipment_totals.get(shipment_id, 0) \
            + amount
    for shipment, shipment_id in enumerate(model['candidate_ids']):
        if shipment_totals.get(shipment_id, 0) > capacities[shipment]:
            raise ValueError(
                'internal error: witness exceeds shipment capacity')

    piece_total = sum(
        amount for (index, shipment), amount in allocation.items()
        if shipment in target_set)
    if piece_total != target_units:
        raise ValueError(
            'internal error: witness metric value does not match endpoint')

    return {
        'units': target_units,
        'shipments_touched': len(shipment_totals),
        'allocation': rows,
        'shipment_totals': {
            shipment_id: shipment_totals[shipment_id]
            for shipment_id in sorted(shipment_totals)},
    }


def _metric_endpoints(model, target_shipments):
    """Exact ``(minimum, maximum)`` pieces on ``target_shipments``."""
    all_shipments = set(range(len(model['candidate_ids'])))
    target = set(target_shipments)
    complement = all_shipments - target
    total_demand = sum(row['units'] for row in model['returns'])
    maximum = _restricted_maxflow(model, target)
    complement_max = _restricted_maxflow(model, complement)
    minimum = total_demand - complement_max
    return minimum, maximum


def _metric_report(model, target_shipments):
    target = set(target_shipments)
    minimum, maximum = _metric_endpoints(model, target)
    # One witness per endpoint; the two may (and need not) agree.
    witness_min = _witness(model, target, minimum)
    witness_max = _witness(model, target, maximum)
    return {
        'target_shipment_ids': sorted(
            model['candidate_ids'][index] for index in target),
        'min': witness_min,
        'max': witness_max,
    }


def _analyze(model, mart, returns_bundle):
    total_demand = sum(row['units'] for row in model['returns'])
    feasible = _feasibility(model)

    base = {
        'format': REPORT_FORMAT,
        'inputs': {
            'mart_format': mart['format'],
            'mart_logic_snapshot_sha256': mart['logic_snapshot_sha256'],
            'returns_sha256': returns_bundle['sha256'],
        },
        'totals': {
            'returns': len(model['returns']),
            'return_units': total_demand,
            'candidate_shipments': len(model['candidate_ids']),
            'shipments_in_mart': len(mart['shipments']),
        },
    }

    if not feasible:
        base['status'] = 'infeasible'
        base['metrics'] = None
        base['infeasibility'] = _infeasibility_certificate(model)
        return WriteoffResult(base)

    base['status'] = 'feasible'
    base['infeasibility'] = None
    metrics = {'affected_union': _metric_report(
        model, model['affected_union'])}
    recalls = {}
    for recall_id in sorted(mart['recall_ids']):
        recalls[recall_id] = _metric_report(
            model, model['affected_by_recall'][recall_id])
    metrics['recalls'] = recalls
    base['metrics'] = metrics
    return WriteoffResult(base)


def report_to_json(report):
    """Stable JSON text for a report (sorted keys, UTF-8, no ASCII escapes)."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))
