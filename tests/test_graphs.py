import unittest

from second_scribe.graphs import structure
from second_scribe.schemas import Edge, MapGraph, Node


def chain(n):
    return MapGraph("c", "x", [Node(f"n{i}", f"s{i}") for i in range(n)],
                    [Edge(f"n{i}", f"n{i+1}") for i in range(n - 1)])


class TestGraphs(unittest.TestCase):
    def test_chain_is_linear(self):
        s = structure(chain(5), glued_only=True)
        self.assertEqual(s["branching_share"], 0.0)
        self.assertEqual(s["longest_path_ratio"], 1.0)
        self.assertEqual(s["n_nontrivial_scc"], 0)
        self.assertEqual(s["cyclomatic"], 0)
        self.assertEqual((s["n_nodes"], s["n_edges"]), (5, 4))
        self.assertAlmostEqual(s["dead_end_share"], 1 / 5)     # only the last node
        self.assertAlmostEqual(s["density"], 4 / 20)

    def test_loop_and_branch(self):
        g = chain(4)
        g.edges += [Edge("n3", "n1"), Edge("n0", "n2")]
        s = structure(g, glued_only=True)
        self.assertEqual(s["n_nontrivial_scc"], 1)
        self.assertGreater(s["branching_share"], 0)
        self.assertEqual(s["cyclomatic"], 5 - 4 + 1)
        # SCC {n1,n2,n3} collapses: condensation path n0 -> SCC has 2 nodes of 4
        self.assertAlmostEqual(s["longest_path_ratio"], 2 / 4)
        self.assertEqual(s["dead_end_share"], 0.0)             # n3 now has an out edge
        self.assertAlmostEqual(s["branching_share"], 1 / 4)    # only n0 has out >= 2, of 4 nodes with out >= 1

    def test_glued_only_drops_geometry_edges(self):
        g = chain(3)
        g.edges[1].glued = False
        self.assertEqual(structure(g, glued_only=True)["n_edges"], 1)
        self.assertEqual(structure(g, glued_only=False)["n_edges"], 2)

    def test_branching_share_definition(self):
        # n0 -> n1, n0 -> n2, n1 -> n3: out>=1 nodes are n0, n1; out>=2 is n0 -> 1/2
        g = MapGraph("b", "x", [Node(f"n{i}", "t") for i in range(4)],
                     [Edge("n0", "n1"), Edge("n0", "n2"), Edge("n1", "n3")])
        s = structure(g, glued_only=True)
        self.assertEqual(s["branching_share"], 0.5)
        self.assertAlmostEqual(s["dead_end_share"], 2 / 4)     # n2, n3
        self.assertEqual(s["longest_path_ratio"], 3 / 4)

    def test_self_loop_and_duplicate_edges_and_dangling(self):
        g = chain(2)
        g.edges += [Edge("n0", "n1"), Edge("n1", "n1"), Edge("n0", "ghost")]
        s = structure(g, glued_only=True)
        self.assertEqual(s["n_edges"], 2)                      # n0->n1 once, n1->n1; ghost dropped
        self.assertEqual(s["n_nontrivial_scc"], 1)             # the self-loop

    def test_density_ignores_self_loops(self):
        g = chain(3)
        base = structure(g, glued_only=True)["density"]
        g.edges.append(Edge("n1", "n1"))
        s = structure(g, glued_only=True)
        self.assertEqual(s["density"], base)                   # 2 / (3 * 2)
        self.assertEqual(s["n_edges"], 3)                      # the self-loop is still an edge
        full = MapGraph("f", "x", [Node(f"n{i}", "t") for i in range(3)],
                        [Edge(f"n{i}", f"n{j}") for i in range(3) for j in range(3)])
        self.assertEqual(structure(full, glued_only=True)["density"], 1.0)    # never above 1

    def test_shares_and_types(self):
        g = MapGraph("m", "x", [Node("a", "t", node_type="service", status="wished"),
                                Node("b", "t", node_type="actor"), Node("c", "t", node_type="service"),
                                Node("d", "t")], [])
        s = structure(g, glued_only=True)
        self.assertEqual(s["wished_share"], 0.25)
        self.assertEqual(s["type_shares"]["service"], 0.5)
        self.assertEqual(s["type_shares"]["actor"], 0.25)
        self.assertEqual(s["type_shares"]["scenario"], 0.0)    # every NODE_TYPE is present
        self.assertEqual(s["cyclomatic"], 0 - 4 + 4)           # 4 isolated components

    def test_empty_graph(self):
        s = structure(MapGraph("e", "x"), glued_only=True)
        self.assertEqual(s["n_nodes"], 0)
        self.assertEqual(s["longest_path_ratio"], 0.0)
        self.assertEqual(s["density"], 0.0)


if __name__ == "__main__":
    unittest.main()
