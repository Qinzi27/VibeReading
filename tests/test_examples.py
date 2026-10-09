"""Check the independently authored nine-language examples against their manifest."""

import json
import unittest
from pathlib import Path

from vibereading.analyzer import analyze_project


class PolyglotExamplesTest(unittest.TestCase):
    def test_expected_symbols_and_call_pairs(self):
        examples = Path(__file__).resolve().parents[1] / "examples"
        expected = json.loads((examples / "expected.json").read_text(encoding="utf-8"))
        graph = analyze_project(examples / expected["project_root"])
        self.assertFalse(graph["diagnostics"], graph["diagnostics"])
        nodes = {(n["file"], n["qualified_name"]): n for n in graph["nodes"]}
        by_id = {n["id"]: n for n in graph["nodes"]}
        edges = {(by_id[e["source"]]["file"], by_id[e["source"]]["qualified_name"],
                  by_id[e["target"]]["file"], by_id[e["target"]]["qualified_name"]): e
                 for e in graph["edges"]}
        self.assertEqual(len(nodes), expected["expected_function_count"])
        self.assertEqual(len(edges), expected["expected_distinct_internal_call_pairs"])
        self.assertEqual(len({n["language"] for n in nodes.values()}), 9)
        for language in expected["languages"]:
            for symbol in language["functions"]:
                key = (symbol["file"], symbol["qualified_name"])
                with self.subTest(language=language["language"], symbol=key):
                    self.assertTrue(key in nodes, f"Missing symbol: {key}")
                    self.assertTrue(nodes[key]["doc"], "Expected explanatory comments were not extracted")
            for call in language["calls"]:
                key = (call["from_file"], call["from"], call["to_file"], call["to"])
                with self.subTest(call=key):
                    self.assertTrue(key in edges, f"Missing call: {key}")
                    self.assertIn(edges[key]["status"], call.get("allowed_statuses", ["resolved"]))


if __name__ == "__main__":
    unittest.main()
