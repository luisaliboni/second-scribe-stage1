import unittest
from second_scribe.schemas import MapGraph, Node, Edge, Transcript, Turn

class TestSchemas(unittest.TestCase):
    def test_mapgraph_roundtrip(self):
        g = MapGraph(map_id="SYN-OW01", origin="synthetic_gold",
                     nodes=[Node("n1", "Fire inspection", "service", "Permits", "experienced"),
                            Node("n2", "One-stop office", "service", None, "wished")],
                     edges=[Edge("n1", "n2", glued=False)], emotion_word="Tired", interviewer="I1")
        g2 = MapGraph.from_json(g.to_json())
        self.assertEqual(g, g2)
        self.assertEqual(g2.node("n2").status, "wished")

    def test_edge_label_roundtrips_and_old_json_still_loads(self):
        g = MapGraph(map_id="SYN-OW01", origin="synthetic_gold", nodes=[Node("a", "A"), Node("b", "B")],
                     edges=[Edge("a", "b", label="six weeks"), Edge("b", "a")])
        self.assertEqual(MapGraph.from_json(g.to_json()), g)
        old = '{"map_id": "x", "origin": "ai", "edges": [{"source": "a", "target": "b", "status": "experienced", "glued": true, "quote": null}]}'
        self.assertIsNone(MapGraph.from_json(old).edges[0].label)

    def test_node_missing_raises(self):
        with self.assertRaises(KeyError):
            MapGraph(map_id="x", origin="ai").node("nope")

    def test_transcript_arms(self):
        t = Transcript("SYN-OW01", [Turn(0, "interviewer", "Let me add a box for that.", True),
                                    Turn(1, "participant", "We waited six weeks.")], "synthetic")
        self.assertEqual(t.text("full"), "[I] Let me add a box for that.\n[P] We waited six weeks.")
        self.assertEqual(t.text("removed"), "[P] We waited six weeks.")

    def test_transcript_speaker_inference_default_and_old_dict_compat(self):
        from dataclasses import asdict
        t = Transcript("SYN-OW01", [Turn(0, "participant", "hi")], "synthetic")
        self.assertEqual(t.speaker_inference, "label")
        d = asdict(t)
        d.pop("speaker_inference")                      # a record written before the field existed
        d["turns"] = [Turn(**x) for x in d["turns"]]
        self.assertEqual(Transcript(**d), t)
