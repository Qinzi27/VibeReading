"""Behavioral fixtures for source navigation and conservative graph binding."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vibereading.analyzer import MAX_FILE_BYTES, SUPPORTED_LANGUAGES, analyze_project


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        # Use project-local scratch space; Windows sandbox temp ACLs may differ.
        self.scratch = Path(__file__).resolve().parents[1] / "work" / "analyzer-tests"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=self.scratch)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.scratch.resolve()))
        self.temp.cleanup()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def graph(self):
        graph = analyze_project(self.root)
        # Serialization verifies internal tree nodes/sets never escape the API.
        json.dumps(graph, ensure_ascii=False)
        return graph

    @staticmethod
    def pairs(graph):
        names = {n["id"]: n["file"] + ":" + n["qualified_name"] for n in graph["nodes"]}
        return {(names[e["source"]], names[e["target"]], e["status"]) for e in graph["edges"]}

    def test_nine_language_definitions_and_direct_calls(self):
        samples = {
            "a.py": "def helper(x):\n return x\ndef run(x):\n return helper(x)\n",
            "b.R": "helper <- function(x) x\nrun <- function(x) helper(x)\n",
            "Box.java": "class Box { static int helper(int x) { return x; } static int run(int x) { return helper(x); } }",
            "a.c": "int helper(int x) { return x; } int run(int x) { return helper(x); }",
            "a.cpp": "namespace N { int helper(int x) { return x; } int run(int x) { return helper(x); } }",
            "a.rs": "fn helper(x: i32) -> i32 { x } fn run(x: i32) -> i32 { helper(x) }",
            "a.js": "function helper(x) { return x; } const run = (x) => helper(x);",
            "a.ts": "function helper(x: number): number { return x; } const run = (x: number) => helper(x);",
            "a.go": "package demo\nfunc helper(x int) int { return x }\nfunc run(x int) int { return helper(x) }",
        }
        for path, text in samples.items():
            self.write(path, text)
        graph = self.graph()
        self.assertEqual(set(SUPPORTED_LANGUAGES), {n["language"] for n in graph["nodes"]})
        self.assertEqual(len(graph["nodes"]), 18)
        self.assertEqual(len(graph["edges"]), 9)
        self.assertFalse(graph["diagnostics"])
        self.assertTrue(all(e["status"] == "resolved" for e in graph["edges"]))

    def test_python_explicit_import_aliases_do_not_mix_same_names(self):
        self.write("geometry.py", "def norm(x):\n return x\n")
        self.write("labels.py", "def norm(x):\n return str(x)\n")
        self.write("main.py", "from geometry import norm\nfrom labels import norm as label_norm\ndef run(x):\n return norm(x), label_norm(x)\n")
        pairs = self.pairs(self.graph())
        self.assertIn(("main.py:run", "geometry.py:norm", "resolved"), pairs)
        self.assertIn(("main.py:run", "labels.py:norm", "resolved"), pairs)
        self.assertEqual(len(pairs), 2)

    def test_python_relative_package_import_and_module_alias(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/utils.py", "def helper(x):\n return x\n")
        self.write("pkg/main.py", "from .utils import helper as h\nimport pkg.utils as util\ndef run(x):\n return h(x) + util.helper(x)\n")
        graph = self.graph()
        self.assertEqual(len(graph["edges"]), 2)
        self.assertTrue(all(e["target"] == "pkg/utils.py::helper" and e["status"] == "resolved" for e in graph["edges"]))

    def test_nested_definition_shadows_module_import(self):
        self.write("util.py", "def helper():\n return 1\n")
        self.write("a.py", "from util import helper\ndef outer():\n def helper():\n  return 2\n return helper()\n")
        graph = self.graph()
        self.assertEqual(self.pairs(graph), {("a.py:outer", "a.py:outer.helper", "resolved")})

    def test_imported_symbol_rebound_at_origin_stays_unknown(self):
        self.write("util.py", "def helper():\n return 1\nhelper = factory()\n")
        self.write("a.py", "from util import helper\ndef run():\n return helper()\n")
        graph = self.graph()
        self.assertFalse(graph["edges"])
        self.assertIn("重赋值", graph["unresolved"][0]["reason"])

    def test_script_local_import_is_candidate_when_root_differs(self):
        self.write("scripts/utils.py", "def helper():\n return 1\n")
        self.write("scripts/main.py", "from utils import helper\ndef run():\n return helper()\n")
        graph = self.graph()
        self.assertEqual(graph["edges"][0]["status"], "candidate")
        rooted = analyze_project(self.root / "scripts")
        self.assertEqual(rooted["edges"][0]["status"], "resolved")

    def test_no_unrelated_global_same_name_match(self):
        self.write("a.py", "def sqrt(x):\n return x\n")
        self.write("b.py", "import math\ndef run(x):\n return sqrt(x) + math.sqrt(x)\n")
        graph = self.graph()
        self.assertFalse(graph["edges"])
        self.assertEqual({u["name"] for u in graph["unresolved"]}, {"sqrt", "math.sqrt"})

    def test_python_callbacks_and_reassigned_module_bindings_stay_unknown(self):
        self.write("a.py", "def helper(x):\n return x\ndef run(helper):\n return helper(1)\ndef other():\n return helper(2)\nhelper = runtime_factory()\n")
        graph = self.graph()
        self.assertFalse(graph["edges"])
        self.assertEqual(len(graph["unresolved"]), 2)

    def test_c_function_pointer_parameter_is_not_project_function(self):
        self.write("a.c", "int helper(int x) { return x; } int run(int (*helper)(int)) { return helper(1); }")
        graph = self.graph()
        self.assertFalse(graph["edges"])
        self.assertEqual(graph["unresolved"][0]["name"], "helper")

    def test_r_callback_parameter_is_unknown(self):
        self.write("a.R", "helper <- function(x) x\nrun <- function(helper) helper(1)\n")
        graph = self.graph()
        self.assertFalse(graph["edges"])
        self.assertEqual(graph["unresolved"][0]["name"], "helper")

    def test_javascript_rebinding_does_not_keep_old_function_target(self):
        self.write("a.js", "function helper(x) { return x; } helper = factory(); function run(x) { return helper(x); }")
        graph = self.graph()
        self.assertFalse(graph["edges"])
        self.assertIn("重赋值", graph["unresolved"][0]["reason"])

    def test_nested_arrow_function_reassignment_remains_unknown(self):
        self.write("a.js", "function run(x) { let helper = (y) => y; helper = factory(); return helper(x); }")
        graph = self.graph()
        self.assertFalse(graph["edges"])
        self.assertEqual(len(graph["nodes"]), 2)

    def test_dynamic_receiver_does_not_match_arbitrary_methods(self):
        self.write("a.py", "class A:\n def helper(self):\n  return 1\n def run(self, obj):\n  return obj.helper() + self.helper()\n")
        graph = self.graph()
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["status"], "candidate")
        self.assertEqual(graph["edges"][0]["label"], "self.helper")
        self.assertEqual(graph["unresolved"][0]["name"], "obj.helper")

    def test_anonymous_callback_body_is_not_outer_direct_call(self):
        self.write("a.py", "def helper(x):\n return x\ndef run(xs):\n return map(lambda x: helper(x), xs)\n")
        graph = self.graph()
        self.assertFalse(graph["edges"])
        self.assertIn("<anonymous callback>", {u["name"] for u in graph["unresolved"]})

    def test_nested_scope_uses_nearest_symbol_and_preserves_container(self):
        self.write("a.py", "def helper():\n return 0\ndef outer():\n def helper():\n  return 1\n return helper()\n")
        graph = self.graph()
        self.assertIn(("a.py:outer", "a.py:outer.helper", "resolved"), self.pairs(graph))
        nested = next(n for n in graph["nodes"] if n["qualified_name"] == "outer.helper")
        self.assertEqual(nested["container"], "outer")

    def test_unicode_paths_comments_and_stable_id_after_line_insertion(self):
        text = '# 原始说明\ndef 求和(x):\n """保留原始文档。"""\n return x + 1\n'
        self.write("数据/计算.py", text)
        before = self.graph()["nodes"][0]
        self.assertIn("原始说明", before["doc"])
        self.assertIn("保留原始文档", before["doc"])
        self.assertEqual(before["line"], 2)
        self.write("数据/计算.py", "\n\n" + text)
        after = self.graph()["nodes"][0]
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(after["line"], 4)
        self.assertNotEqual(before["code_hash"], after["code_hash"])

    def test_duplicate_definitions_are_distinct_candidates(self):
        self.write("a.py", "def helper():\n return 1\ndef helper():\n return 2\ndef run():\n return helper()\n")
        graph = self.graph()
        self.assertEqual(len({n["id"] for n in graph["nodes"]}), 3)
        self.assertEqual(len(graph["edges"]), 2)
        self.assertTrue(all(e["status"] == "candidate" for e in graph["edges"]))

    def test_syntax_errors_are_explicit_and_edges_not_claimed_resolved(self):
        self.write("a.py", "def helper():\n return 1\ndef run():\n return helper()\nbroken = (\n")
        graph = self.graph()
        self.assertTrue(graph["files"][0]["error"])
        self.assertTrue(graph["diagnostics"])
        self.assertTrue(all(e["status"] == "candidate" for e in graph["edges"]))

    def test_ignored_directories_size_limit_and_file_removal(self):
        source = self.write("a.py", "def visible():\n return 1\n")
        self.write(".venv/hidden.py", "def hidden():\n pass\n")
        self.write("work/scratch.py", "def scratch():\n pass\n")
        self.write("large.py", "#" * (MAX_FILE_BYTES + 1))
        graph = self.graph()
        self.assertEqual({n["name"] for n in graph["nodes"]}, {"visible"})
        self.assertTrue(next(f for f in graph["files"] if f["path"] == "large.py")["error"])
        # Only this disposable test fixture is removed; no inspected source is changed by the analyzer.
        source.unlink()
        self.assertFalse(self.graph()["nodes"])

    def test_import_and_definition_name_collision_is_unresolved(self):
        self.write("util.py", "def helper():\n return 1\n")
        self.write("a.py", "from util import helper\ndef helper():\n return 2\ndef run():\n return helper()\n")
        graph = self.graph()
        self.assertFalse(graph["edges"])
        self.assertIn("同名", graph["unresolved"][0]["reason"])

    def test_analyzer_can_read_its_own_nontrivial_source_without_native_crash(self):
        # Keep a native parser failure in a child process so unittest reports a
        # failure rather than abruptly terminating the entire test run.
        app_root = Path(__file__).resolve().parents[1]
        command = (
            "from pathlib import Path; import json; "
            "from vibereading.analyzer import analyze_project; "
            "g=analyze_project(Path('vibereading')); "
            "print(json.dumps({'stats':g['stats'],'errors':[f['error'] for f in g['files'] if f['error']]}))"
        )
        result = subprocess.run([sys.executable, "-X", "utf8", "-X", "faulthandler", "-c", command], cwd=app_root, capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        report = json.loads(result.stdout)
        self.assertGreater(report["stats"]["functions"], 20)
        self.assertFalse(report["errors"])


if __name__ == "__main__":
    unittest.main()
