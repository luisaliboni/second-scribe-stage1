"""Structure measures of a map, computed on node ids and edges only (no text)."""
from __future__ import annotations

from .schemas import NODE_TYPES, MapGraph


def _sccs(nodes: list[str], succ: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan, iterative. Components come out in reverse topological order (sinks first)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(succ[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(succ[w])))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out


def structure(g: MapGraph, glued_only: bool) -> dict:
    """Structure of ``g``. Edges: unique (source, target) pairs between existing nodes; with
    ``glued_only`` the geometry-fallback edges (``glued=False``) are dropped. Self-loops count as
    edges. N is the number of nodes; an empty graph gives zeros.

    n_nodes, n_edges, density (E / N(N-1), directed; self-loops are not counted in E here, so it never exceeds 1), branching_share (out>=2 / out>=1),
    n_nontrivial_scc (strongly connected components with >1 node, or one node with a self-loop),
    cyclomatic (E - N + C, C = weakly connected components, isolated nodes included),
    dead_end_share (in>=1 and out=0, over N), longest_path_ratio (longest path in the SCC
    condensation DAG, each component counted as ONE node, over N), wished_share, type_shares
    (one entry per NODE_TYPE)."""
    ids = [n.node_id for n in g.nodes]
    idset = set(ids)
    n = len(ids)
    edges = {(e.source, e.target) for e in g.edges
             if (e.glued or not glued_only) and e.source in idset and e.target in idset}
    type_shares = {t: (sum(1 for x in g.nodes if x.node_type == t) / n if n else 0.0) for t in NODE_TYPES}
    wished = sum(1 for x in g.nodes if x.status == "wished") / n if n else 0.0
    if n == 0:
        return {"n_nodes": 0, "n_edges": 0, "density": 0.0, "branching_share": 0.0, "n_nontrivial_scc": 0,
                "cyclomatic": 0, "dead_end_share": 0.0, "longest_path_ratio": 0.0,
                "wished_share": 0.0, "type_shares": type_shares}
    succ: dict[str, list[str]] = {i: [] for i in ids}
    outdeg = {i: 0 for i in ids}
    indeg = {i: 0 for i in ids}
    for s, t in sorted(edges):
        succ[s].append(t)
        outdeg[s] += 1
        indeg[t] += 1
    has_out = sum(1 for i in ids if outdeg[i] >= 1)
    branching = sum(1 for i in ids if outdeg[i] >= 2) / has_out if has_out else 0.0
    dead_ends = sum(1 for i in ids if indeg[i] >= 1 and outdeg[i] == 0)

    comps = _sccs(ids, succ)
    comp_of = {v: k for k, comp in enumerate(comps) for v in comp}
    nontrivial = sum(1 for comp in comps if len(comp) > 1 or (comp[0], comp[0]) in edges)
    # comps is in reverse topological order: every edge goes from a higher index to a lower one
    longest = [1] * len(comps)
    dag: dict[int, set[int]] = {k: set() for k in range(len(comps))}
    for s, t in edges:
        if comp_of[s] != comp_of[t]:
            dag[comp_of[s]].add(comp_of[t])
    for k in range(len(comps)):                      # sinks first, so successors are final
        for m in dag[k]:
            longest[k] = max(longest[k], 1 + longest[m])

    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for s, t in edges:
        parent[find(s)] = find(t)
    weak = len({find(i) for i in ids})
    return {
        "n_nodes": n, "n_edges": len(edges),
        "density": sum(1 for s_, t_ in edges if s_ != t_) / (n * (n - 1)) if n > 1 else 0.0,
        "branching_share": branching, "n_nontrivial_scc": nontrivial,
        "cyclomatic": len(edges) - n + weak,
        "dead_end_share": dead_ends / n, "longest_path_ratio": max(longest) / n,
        "wished_share": wished, "type_shares": type_shares,
    }
