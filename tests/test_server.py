"""Verify saved-file refresh, annotation history and local HTTP boundaries."""

import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from vibereading.server import LocalServer, ProjectSession
from vibereading.storage import AnnotationStore, ConflictError


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parents[1] / "work"
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="server-test-", dir=work)
        self.root = Path(self.temp.name)
        self.project = self.root / "中文项目"
        self.project.mkdir()
        self.source = self.project / "demo.py"
        self.source.write_text('def square(x):\n    """Return a square."""\n    return x*x\n', encoding="utf-8")
        self.session = ProjectSession(self.project, self.root / "state.sqlite3", interval=0.08)

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def node(self):
        return next(n for n in self.session.snapshot()["nodes"] if n["name"] == "square")

    def save(self, **kwargs):
        node = self.node()
        data = dict(project_root=str(self.session.root), id=node["id"], code_hash=node["code_hash"], expected_version=0,
                    summary="计算平方", inputs="数值", outputs="平方", notes="示例", origin="manual", status="approved")
        data.update(kwargs)
        return self.session.save_annotation(data)

    def test_annotation_history_and_conflicts(self):
        first = self.save()["annotation"]
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["status"], "approved")
        with self.assertRaises(ConflictError):
            self.save(summary="过期更新")
        second = self.save(expected_version=1, summary="平方值")["annotation"]
        self.assertEqual(second["version"], 2)
        with closing(sqlite3.connect(self.root / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM annotations").fetchone()[0], 2)

    def test_save_refresh_and_annotation_stale(self):
        self.save()
        initial = self.session.status()["revision"]
        self.source.write_text('def square(x):\n    """Return a square plus one."""\n    return x*x+1\n', encoding="utf-8")
        deadline = time.monotonic() + 6
        while self.session.status()["revision"] == initial and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreater(self.session.status()["revision"], initial)
        self.assertEqual(self.node()["annotation"]["status"], "stale")
        self.assertIn("x*x+1", self.node()["code"])

    def test_unsaved_code_hash_rejected_even_before_watcher(self):
        node = self.node()
        self.source.write_text('def square(x):\n    return x**3\n', encoding="utf-8")
        with self.assertRaises(ConflictError):
            self.save(code_hash=node["code_hash"])

    def test_import_never_autoapproves_or_replaces_existing(self):
        node = self.node()
        result = self.session.import_annotations([dict(id=node["id"], summary="AI draft", status="approved")], str(self.session.root))
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.node()["annotation"]["status"], "draft")
        self.assertEqual(self.node()["annotation"]["origin"], "ai")
        again = self.session.import_annotations([dict(id=node["id"], summary="overwrite")], str(self.session.root))
        self.assertEqual(again["imported"], 0)
        self.assertEqual(self.node()["annotation"]["summary"], "AI draft")

    def test_project_switch_keeps_independent_annotations(self):
        self.save()
        another = self.root / "other"
        another.mkdir()
        (another / "demo.py").write_text(self.source.read_text(encoding="utf-8"), encoding="utf-8")
        self.session.open_project(another)
        self.assertIsNone(self.node()["annotation"])
        self.session.open_project(self.project)
        self.assertEqual(self.node()["annotation"]["summary"], "计算平方")

    def test_annotation_survives_service_session_restart(self):
        source_before = self.source.read_bytes()
        self.save()
        self.session.close()
        self.session = ProjectSession(self.project, self.root / "state.sqlite3", interval=0.08)
        self.assertEqual(self.node()["annotation"]["summary"], "计算平方")
        self.assertEqual(self.node()["annotation"]["status"], "approved")
        self.assertEqual(self.source.read_bytes(), source_before)

    def test_http_same_origin_and_traversal(self):
        server = LocalServer(("127.0.0.1", 0), self.session)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(address + "/api/graph") as response:
                self.assertEqual(len(json.load(response)["nodes"]), 1)
            blocked = urllib.request.Request(address + "/api/project", data=b'{"path":"C:/"}',
                headers={"Content-Type": "application/json", "Origin": "https://example.invalid"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(blocked)
            self.assertEqual(caught.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(address + "/../run.py")
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
