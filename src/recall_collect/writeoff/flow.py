"""Integer max-flow kernel used by the write-off interval analysis.

Capacities are arbitrary-precision integers and every augmentation pushes the
path bottleneck, so a shipment holding one billion units is moved in one
step.  The analysis never expands a quantity into individual pieces and
never enumerates allocation schemes.

The kernel decides feasibility and exposes the residual graph: the
reachable set after a failed flow directly yields a Hall-violating return
subset for the infeasibility certificate, and a bounded circulation built
on the same network materialises a complete allocation witness.
"""
import sys

INF = 10 ** 30 << 64  # far above any total this analysis can produce


class Dinic:
    """Max flow with explicit residual access.

    Each edge is stored as ``[target, reverse_index, residual, capacity]``.
    ``edge_flow(handle)`` returns the amount currently carried by a forward
    edge, which the witness builder uses to read allocations back out of a
    feasibility circulation.
    """

    def __init__(self, node_count):
        self.n = node_count
        self.graph = [[] for _ in range(node_count)]

    def add_edge(self, u, v, capacity):
        """Add ``u -> v``; return a forward handle ``(u, edge_index)``."""
        if capacity < 0:
            raise ValueError('edge capacity must be non-negative')
        forward = [v, len(self.graph[v]), capacity, capacity]
        self.graph[u].append(forward)
        reverse = [u, len(self.graph[u]) - 1, 0, 0]
        self.graph[v].append(reverse)
        return u, len(self.graph[u]) - 1

    def edge_flow(self, handle):
        u, index = handle
        edge = self.graph[u][index]
        return edge[3] - edge[2]

    def _levels(self, source, sink):
        level = [-1] * self.n
        level[source] = 0
        queue = [source]
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            next_level = level[u] + 1
            for edge in self.graph[u]:
                if edge[2] > 0 and level[edge[0]] < 0:
                    level[edge[0]] = next_level
                    queue.append(edge[0])
        return level if level[sink] >= 0 else None

    def max_flow(self, source, sink, limit=INF):
        total = 0
        if source == sink:
            return 0
        # Residual augmenting paths can alternate return/shipment and grow
        # to graph depth; lift the recursion ceiling for the call only.
        previous_limit = sys.getrecursionlimit()
        if previous_limit < self.n * 2 + 200:
            sys.setrecursionlimit(self.n * 2 + 200)
        try:
            while total < limit:
                level = self._levels(source, sink)
                if level is None:
                    break
                cursor = [0] * self.n

                def push(u, available):
                    if u == sink:
                        return available
                    while cursor[u] < len(self.graph[u]):
                        edge = self.graph[u][cursor[u]]
                        v = edge[0]
                        if edge[2] > 0 and level[v] == level[u] + 1:
                            sent = push(v, min(available, edge[2]))
                            if sent:
                                edge[2] -= sent
                                self.graph[v][edge[1]][2] += sent
                                return sent
                        cursor[u] += 1
                    return 0

                while True:
                    sent = push(source, limit - total)
                    if not sent:
                        break
                    total += sent
        finally:
            sys.setrecursionlimit(previous_limit)
        return total

    def residual_reachable(self, source):
        """Node set reachable from ``source`` along residual edges."""
        seen = {source}
        queue = [source]
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            for edge in self.graph[u]:
                if edge[2] > 0 and edge[0] not in seen:
                    seen.add(edge[0])
                    queue.append(edge[0])
        return seen

