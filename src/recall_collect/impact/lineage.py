"""Lineage graph validation and conservative recall propagation.

Propagation rule (by design, no probability or quantity weighting):

* a matched notice batch is a recall root and affects itself at zero hops;
* whenever any input batch of a process is affected, every output batch of
  that process is affected by the same root and the effect flows further
  downstream.

The production graph is required to be acyclic over every component,
including components no recall ever reaches.  A batch that is simultaneously
an input and an output of one process is a self-loop and is rejected as a
cycle.
"""
from .sources import ImpactBuildError


def validate_dag(lots, processes):
    """Kahn topological sort over batch nodes; any residual node is a cycle."""
    indegree = {lot: 0 for lot in lots}
    downstream = {lot: [] for lot in lots}
    edges = set()
    for process_id, proc in processes.items():
        for in_lot in proc['inputs']:
            for out_lot in proc['outputs']:
                edge = (in_lot, out_lot, process_id)
                if edge in edges:
                    # Same process can still connect the same batch pair only
                    # once; duplicate members were already rejected earlier.
                    continue
                edges.add(edge)
                indegree[out_lot] += 1
                downstream[in_lot].append((process_id, out_lot))
    ready = sorted(lot for lot, deg in indegree.items() if deg == 0)
    seen = 0
    while ready:
        lot = ready.pop(0)
        seen += 1
        for _, out_lot in downstream[lot]:
            indegree[out_lot] -= 1
            if indegree[out_lot] == 0:
                ready.append(out_lot)
        ready.sort()
    if seen != len(lots):
        cyclic = sorted(lot for lot, deg in indegree.items() if deg > 0)
        raise ImpactBuildError(
            'production graph contains a cycle; residual batches: %r'
            % (cyclic,))
    return downstream


def resolve_notice_lots(recalls, name_to_product, lots):
    """Classify every (recall, product, lot) in the accepted notices."""
    resolutions = []
    for recall_id in sorted(recalls):
        record = recalls[recall_id]
        notice_product = record['product']
        for lot_code in sorted(set(record['lots'])):
            product_id = name_to_product.get(notice_product)
            if product_id is None:
                status = 'product_unmapped'
                resolved_product = None
            elif (product_id, lot_code) not in lots:
                status = 'lot_unregistered'
                resolved_product = product_id
            else:
                status = 'matched'
                resolved_product = product_id
            resolutions.append({
                'recall_id': recall_id,
                'notice_product': notice_product,
                'notice_lot': lot_code,
                'product_id': resolved_product,
                'lot_code': lot_code,
                'status': status,
            })
    return resolutions


def build_roots(resolutions):
    """One root per distinct matched (recall, product, lot)."""
    roots = {}
    for res in resolutions:
        if res['status'] != 'matched':
            continue
        key = (res['recall_id'], res['product_id'], res['lot_code'])
        roots.setdefault(key, {
            'recall_id': res['recall_id'],
            'product_id': res['product_id'],
            'lot_code': res['lot_code'],
        })
    return roots


def propagate(roots, downstream):
    """Compute one canonical fact per (root, reachable batch).

    Complexity is linear in the graph for each root: every batch holds a
    single best label and is relaxed at most once per improving hop count;
    converging paths merge into that label instead of forking path lists, so
    a diamond with 2**k converging branches never produces 2**k intermediate
    rows.  Paths themselves are never enumerated.

    Label ordering implements the canonical basis:
    1. fewest production processes (hop count);
    2. tie broken by the Unicode code-point lexicographic order of the full
       step sequence, where each step is
       ``(process_id, output product_id, output lot_code)``.
    A label stores only ``(hops, steps, predecessor batch, via process)``;
    the full canonical path is reconstructed by following predecessor links.
    """
    facts = []
    for root_key in sorted(roots):
        recall_id, product_id, lot_code = root_key
        root_lot = (product_id, lot_code)
        # label: (hops, steps tuple, predecessor lot or None, via process)
        best = {root_lot: (0, (), None, None)}
        frontier = {root_lot}
        while frontier:
            current = min(frontier)
            frontier.discard(current)
            cur_hops, cur_steps, _, _ = best[current]
            for process_id, out_lot in sorted(downstream[current]):
                step = (process_id, out_lot[0], out_lot[1])
                candidate = (cur_hops + 1, cur_steps + (step,),
                             current, process_id)
                old = best.get(out_lot)
                if old is None or (candidate[0], candidate[1]) < \
                        (old[0], old[1]):
                    best[out_lot] = candidate
                    frontier.add(out_lot)
        for lot_key in sorted(best):
            hops, steps, predecessor, via_process = best[lot_key]
            facts.append({
                'recall_id': recall_id,
                'root_product_id': product_id,
                'root_lot_code': lot_code,
                'product_id': lot_key[0],
                'lot_code': lot_key[1],
                'hops': hops,
                'predecessor_product_id':
                    predecessor[0] if predecessor is not None else None,
                'predecessor_lot_code':
                    predecessor[1] if predecessor is not None else None,
                'via_process': via_process,
                'canonical_steps': steps,
            })
    return facts
