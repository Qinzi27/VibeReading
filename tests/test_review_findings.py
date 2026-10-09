"""Regression cases from independent review of project identity and saved headers."""

import tempfile
import time
import unittest
from pathlib import Path

from vibereading.server import ProjectSession
from vibereading.storage import ConflictError


class ReviewFindingsTest(unittest.TestCase):
    def setUp(self):
        # Confine every review fixture and its state database to the app workspace.
        work = Path(__file__).resolve().parents[1] / "work"
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="review-test-", dir=work)
        self.root = Path(self.temp.name)
        self.project = self.root / "project-a"
        self.project.mkdir()
        self.session = None

    def tearDown(self):
        if self.session is not None:
            self.session.close()
        self.temp.cleanup()

    def test_saved_cpp_hh_header_refreshes(self):
        """A supported header extension must participate in saved-file watching."""
        source = self.project / "value.hh"
        source.write_text("int value() { return 1; }\n", encoding="utf-8")
        self.session = ProjectSession(self.project, self.root / "state.sqlite3", interval=0.05)
        initial = self.session.snapshot()
        self.assertEqual(len(initial["nodes"]), 1)
        source.write_text("int value() { return 2; }\n", encoding="utf-8")
        deadline = time.monotonic() + 2
        while self.session.status()["revision"] == initial["revision"] and time.monotonic() < deadline:
            time.sleep(0.03)
        refreshed = self.session.snapshot()
        self.assertGreater(refreshed["revision"], initial["revision"])
        self.assertIn("return 2", refreshed["nodes"][0]["code"])

    def test_stale_project_annotation_cannot_land_in_another_project(self):
        """Two tabs/projects with identical symbols must not share a write target."""
        content = "def square(x):\n    return x*x\n"
        (self.project / "demo.py").write_text(content, encoding="utf-8")
        another = self.root / "project-b"
        another.mkdir()
        (another / "demo.py").write_text(content, encoding="utf-8")
        self.session = ProjectSession(self.project, self.root / "state.sqlite3", interval=10)
        graph_a = self.session.snapshot()
        node_a = graph_a["nodes"][0]
        # Carry the source project's identity as well as symbol/hash/version.
        pending_a = {
            "project_root": graph_a["project"]["root"],
            "id": node_a["id"], "code_hash": node_a["code_hash"], "expected_version": 0,
            "summary": "只属于项目 A 的人工解释", "inputs": "", "outputs": "", "notes": "",
            "origin": "manual", "status": "approved",
        }
        self.session.open_project(another)
        with self.assertRaises(ConflictError):
            self.session.save_annotation(pending_a)
        self.assertIsNone(self.session.snapshot()["nodes"][0]["annotation"])
        self.assertEqual(self.session.store.latest(str(self.project.resolve())), {})


if __name__ == "__main__":
    unittest.main()
